# Podman 上的 LiteLLM Gateway（搭配 PostgreSQL 16）

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Podman](https://img.shields.io/badge/Podman-4.9.3%20rootless-892CA0?logo=podman&logoColor=white)](https://podman.io/)
[![Quadlet](https://img.shields.io/badge/units-Quadlet%20%2B%20systemd-orange)](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
[![LiteLLM](https://img.shields.io/badge/LiteLLM-v1.83.14--stable-00A67E)](https://github.com/BerriAI/litellm)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16.15--alpine-336791?logo=postgresql&logoColor=white)](https://www.postgresql.org/)

[English](README.md) · **繁體中文**

---

## 總覽

一個 OpenAI 相容的 **LiteLLM proxy**，後端是 **PostgreSQL 16**，在單一 Linux 主機上以 rootless
**Podman** 執行，並由 **systemd 透過 Quadlet** 監督：兩個容器、一個 bridge 網路、一個具名 volume、
一個唯讀設定檔、一個對外連接埠。它把五個經由 OpenRouter 的模型收斂到單一 `/v1` API，可簽發帶預算與
模型白名單的虛擬金鑰，並把金鑰、團隊、使用者、花費與加密後的憑證存在 PostgreSQL，另外在 `/ui`
提供管理介面。

> **尚未在真實 Podman 主機上執行過。** 目前的驗證都是靜態的（podman 4.9.3 的 Quadlet 產生器、
> `systemd-analyze --user verify`、shellcheck）與以測試替身（shim）進行。第一次實機執行是引入本
> 版面配置之 PR 中的 toypark1234 全新安裝測試；在 `tests/smoke.sh` 於該主機通過之前，請視任何部署
> 為未驗證。

### 為什麼是 Podman，以及它如何對應 k3s 部署

同一個 gateway 已經跑在 k3s 上；本套件是它刻意為單節點所做的轉譯。
[**`docs/k3s-to-podman.md`**](docs/k3s-to-podman.md) 就是那份分析：候選的 Podman 方案、每個
Kubernetes 物件逐一的對應、七個無法轉譯的東西，以及何時該留在 Kubernetes。一句話版本：
*如果「這台機器掛掉會怎樣？」的答案不能是「服務就停到它回來為止」，那你需要 Kubernetes。*

## 特色

| 特色 | 在這裡如何實作 |
|---|---|
| OpenAI 相容 gateway | LiteLLM `v1.83.14-stable`，`/v1/*` 在連接埠 4000 |
| 五個模型走同一個上游 | OpenRouter slug 定義在 `config/config.yaml` |
| 虛擬金鑰、預算、團隊、花費 | PostgreSQL 16.15，`store_model_in_db: true` |
| 管理介面 | `/ui`，帳號 `admin`，密碼就是 master key |
| 重開機後仍存活、rootless | Quadlet 單元 + `[Install] WantedBy=default.target` + `loginctl enable-linger` |
| 以健康狀態把關的啟動順序 | `litellm-postgres` 要等它的 `ExecStartPost=` 確認 Postgres 接受 TCP 連線才算 *active*；proxy 對它 `Requires=` |
| 沒有任何明文憑證 | 五個 podman secret；Postgres 以檔案讀取自己的密碼，proxy 以 env secret 取得其餘 |
| 設定只有一份 | `config/config.yaml` 安裝到 `~/.config/litellm/config.yaml`，以唯讀掛載 |
| 可重複執行的安裝 | 由 0600 env 檔渲染；只寫入有變更的檔案，只重啟對應單元 |
| 驗證、備份、還原、輪替 | `tests/smoke.sh`、`scripts/backup.sh`、`scripts/restore.sh`（salt 指紋把關）、`scripts/rotate-secrets.sh` |
| 刻意不提供對外入口 | 沒有 cloudflared、沒有 tunnel token：對外存取只有文件說明 |

## 架構

```
     API 用戶端  --(HTTP + 虛擬金鑰 sk-...)-->  127.0.0.1:4000（預設）
 PODMAN 主機（rootless, systemd --user）==============================
 |  litellm.service            Requires=/After= litellm-postgres.service |
 |  +-------------------------------------------------------------+  |
 |  | BRIDGE NET litellm-net                                       |  |
 |  |  [ litellm ]  ghcr.io/berriai/litellm:v1.83.14-stable :4000  |<-- ~/.config/litellm/config.yaml（唯讀）
 |  |   |  postgresql://litellm@litellm-postgres:5432（aardvark DNS）|  |
 |  |  [ litellm-postgres ]  postgres:16.15-alpine3.24  不對外開埠  | |
 |  +---|----------------------------------------------------------+  |
 |      v 具名 volume litellm-pgdata（PGDATA=.../pgdata）             |
 ======================|==============================================
      只有對外 HTTPS 443 -> https://openrouter.ai/api/v1
```

啟動流程、請求路徑與單元名稱的推導都在 [**`docs/architecture.md`**](docs/architecture.md)。

| | `litellm` | `litellm-postgres` |
|---|---|---|
| 映像 | `ghcr.io/berriai/litellm:v1.83.14-stable` | `docker.io/library/postgres:16.15-alpine3.24` |
| 對外發布 | `LITELLM_BIND:LITELLM_PORT` -> 4000（預設 `127.0.0.1:4000`） | **從不** |
| Secret | `DATABASE_URL`、`LITELLM_MASTER_KEY`、`LITELLM_SALT_KEY`、`OPENROUTER_API_KEY`（env secret） | 只有自己的密碼，且以檔案形式 |
| 健康檢查 | 啟動：`/health/readiness` 40 × 15 秒；存活：`/health/liveliness` 20 秒 × 6 | `pg_isready` 走 TCP，10 秒 × 3，起始寬限 60 秒 |
| 資源上限 | 2 GiB、2.0 CPU | 1 GiB、1.0 CPU |
| 重啟 | `Restart=always`、`RestartSec=10` | 同上 |

## 先決條件

| 需求 | 內容 |
|---|---|
| Podman | 最低 **4.9**，已在 4.9.3 rootless（Ubuntu 24.04）測試 |
| systemd | 真正的 `systemd --user` 工作階段（有設定 `XDG_RUNTIME_DIR`），linger 由安裝腳本啟用 |
| cgroups / 記憶體 / 磁碟 | v2 / 4 GiB / 約 10 GiB 可用空間 / 2 核心 |
| 網路 | 可對外連到 `ghcr.io`、`docker.io`、`openrouter.ai` 的 HTTPS |
| 帳號 | 一組 OpenRouter API 金鑰（`sk-or-...`），申請處 <https://openrouter.ai/keys> |

```bash
podman --version && podman info --format '{{.Host.CgroupsVersion}}'   # 應為 v2
systemctl --user is-system-running                                    # running / degraded 皆可，不能是 offline
```

rootless 主機預設不會把 `cpu`/`cpuset` controller 委派給 user slice，因此單元中的 `--cpus` 可能無效，
需要管理者加上 drop-in：

```bash
sudo mkdir -p /etc/systemd/system/user@.service.d
printf '[Service]\nDelegate=memory pids cpu cpuset\n' | sudo tee /etc/systemd/system/user@.service.d/delegate.conf
sudo systemctl daemon-reload     # 然後登出「所有」工作階段再重新登入
```

## 安裝

```bash
git clone https://github.com/WOOWTECH/Woow_podman_litellm.git
cd Woow_podman_litellm
scripts/install.sh                      # 會建立 env 檔並停下：它需要你的金鑰
${EDITOR:-vi} ~/.config/litellm/litellm.env      # 設定 OPENROUTER_API_KEY
scripts/install.sh                      # 或：scripts/install.sh --port 18400
```

`scripts/install.sh` 可重複執行，它會：

1. 檢查主機（非 root、podman >= 4.9、有 Quadlet 產生器、`systemctl --user` 可用）並啟用 linger；
2. 第一次執行時由 [`config/litellm.env.example`](config/litellm.env.example) 建立
   `~/.config/litellm/litellm.env`（0600）；`--port N`、`--bind ADDR`、`--set KEY=VALUE` 會修改設定
   並存回該檔；
3. 若已存在不受 Quadlet 管理、名為 `litellm` 或 `litellm-postgres` 的容器（會被
   `podman run --replace` 刪除），或連接埠已被占用，就拒絕繼續；
4. 用該 env 檔渲染 [`quadlet/`](quadlet/) 內的單元（`@@VAR@@` 標記，白名單在 `quadlet/render-vars`），
   並在安裝任何東西「之前」用 podman 4.9.3 產生器與 `systemd-analyze --user verify` 驗證；
5. 預先拉取兩個固定版本的映像，讓緩慢的拉取永遠不會發生在 `TimeoutStartSec` 之內；
6. 建立所有尚不存在的五個 podman secret，並在每次執行時由資料庫密碼推導出 `DATABASE_URL`；
7. 只安裝有變更的檔案、只重啟對應單元，接著等待 Postgres 與 proxy 變成 healthy，最後執行
   [`tests/smoke.sh`](tests/smoke.sh)。

`scripts/install.sh --dry-run` 會渲染、驗證並回報將會變更什麼，但不做任何變更。它是決定性的：
連續執行 30 次會得到 30 次相同結果（舊的 `quadlet/install.sh` 大約每三次就有兩次在合法單元上失敗，
因為它把產生器的 stdout 與 stderr 混在一起 grep `error|failed`，而比對到單元自己的註解）。

安裝的內容：

| 路徑 | 內容 |
|---|---|
| `~/.config/containers/systemd/litellm.container`、`litellm-postgres.container`、`litellm.network`、`litellm-pgdata.volume` | Quadlet 單元 |
| `~/.config/litellm/litellm.env` | 每台主機的設定（0600） |
| `~/.config/litellm/config.yaml` | 模型清單，以唯讀掛載進 proxy |
| podman secret `litellm-{postgres-password,database-url,master-key,salt-key,openrouter-api-key}` | 各項憑證 |

### 設定

編輯 `~/.config/litellm/litellm.env` 後重新執行 `scripts/install.sh`。

| 鍵 | 預設 | 說明 |
|----|------|------|
| `LITELLM_BIND` | `127.0.0.1` | 發布位址。設成別的值就會被該網路連到，見 [對外存取](#對外存取)。 |
| `LITELLM_PORT` | `4000` | 主機連接埠。 |
| `LITELLM_LOG` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`。 |
| `OPENROUTER_API_KEY` | *（第一次必填）* | 會被複製進 secret；換新值會重啟 proxy。之後可以清空。 |
| `LITELLM_MASTER_KEY` | *（自動產生）* | 可選擇匯入既有值，必須以 `sk-` 開頭。 |
| `LITELLM_SALT_KEY` | *（自動產生）* | 可選擇匯入既有值；secret 一旦存在就 **永不替換**。 |

這些金鑰只會從這個 0600 檔案流向 podman secret。安裝完成後你可以把它們清空，secret 會留著。
要讀回 master key：

```bash
podman secret inspect --showsecret --format '{{.SecretData}}' litellm-master-key
```

### Secret 模型

| Secret | 使用者 | 方式 |
|---|---|---|
| `litellm-postgres-password` | `litellm-postgres` | `type=mount` + `POSTGRES_PASSWORD_FILE`；`podman inspect` 看不到 |
| `litellm-database-url` | `litellm` | `type=env DATABASE_URL`，每次安裝由密碼推導 |
| `litellm-master-key` | `litellm` | `type=env`；`/v1` 的管理憑證，也是管理介面密碼 |
| `litellm-salt-key` | `litellm` | `type=env`；加密資料庫中的供應商憑證。**設定一次，永不輪替** |
| `litellm-openrouter-api-key` | `litellm` | `type=env`；`config.yaml` 以 `os.environ/OPENROUTER_API_KEY` 取用 |

這些值不在 git、不在單元檔、不在 `systemctl --user cat`、不在容器的建立指令、也不在 journal 裡。
但在 podman 4.9.3 上，`type=env` 的 secret **會** 出現在執行中容器的 `podman inspect`，因此能使用這個
使用者 podman socket 的人都讀得到那四個值。遺失 `LITELLM_SALT_KEY` 會讓資料庫中所有供應商憑證永遠
無法解密：`scripts/backup.sh` 會把它寫進 `secrets.env`，請另外保存一份在這台主機以外。

## 第一批請求

```bash
KEY=$(podman secret inspect --showsecret --format '{{.SecretData}}' litellm-master-key)
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:4000/v1/models | head -c 400
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  http://127.0.0.1:4000/key/generate -d '{"models":["gpt-4o-mini"],"max_budget":5,"key_alias":"demo"}'
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  http://127.0.0.1:4000/v1/chat/completions \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"ping"}]}'
```

請簽發範圍受限的虛擬金鑰，而不是把 master key 發給別人。管理介面在
`http://127.0.0.1:4000/ui`（帳號 `admin`，密碼就是 master key）。

**新增或修改模型**：編輯 `config/config.yaml`，然後執行 `scripts/install.sh`，已安裝的副本會更新並
重啟 proxy。新增 slug 前請先對照線上目錄（`curl -s https://openrouter.ai/api/v1/models`）：
OpenRouter 會下架 slug，本 repo 就曾因為 `anthropic/claude-3.5-sonnet` 這個裸 slug 而踩雷。另外，
來自檔案與來自資料庫的 `model_list` 是 **合併** 而非取代：在 `config.yaml` 定義的模型無法從管理介面
刪除。`config/config.yaml` 與 k3s 部署逐位元組共用，CI 會固定它的雜湊，請兩邊一起改。

## 日常操作

```bash
systemctl --user list-units 'litellm*'
journalctl --user -u litellm.service -u litellm-postgres.service -f
podman inspect --format '{{.State.Health.Status}}' litellm litellm-postgres
curl -s http://127.0.0.1:4000/health/liveliness    # 只看行程，不需金鑰、不碰資料庫
curl -s http://127.0.0.1:4000/health/readiness     # 會檢查資料庫；資料庫掛掉時回 503
tests/smoke.sh                                     # 完整的安裝後檢查
systemctl --user restart litellm.service
```

> **絕對不要去探測純 `/health`。** 它需要金鑰，而且會對每個設定過的模型送出一次真實請求，
> 每次輪詢都在燒 OpenRouter 額度。另外，**LiteLLM 容器裡沒有 `curl`**（映像只有 Python），
> 這也是容器內健康檢查都用 `python -c` 單行指令的原因。

## 升級

```bash
git pull          # 帶來新的 Image= 版本
scripts/upgrade.sh
```

它會先為已安裝的單元建立快照、先做一次 `pg_dump`（LiteLLM 的 Prisma migration 是單向的），
再執行 `scripts/install.sh` 與 `tests/smoke.sh`；任何一步失敗就放回先前的單元、用先前的映像重啟，
並告訴你如何還原那份傾印檔。

## 備份、還原與輪替

```bash
scripts/backup.sh                          # -> ~/backups/litellm/<時間戳>/（目錄 0700、檔案 0600）
scripts/restore.sh ~/backups/litellm/<時間戳>              # 會 DROP 並重建資料庫
scripts/restore.sh <目錄> --with-secrets                    # 還原到另一台主機：一併採用備份的 salt key
scripts/rotate-secrets.sh --db | --master                  # --salt 會被拒絕，並說明原因
```

備份包含 `pg_dump -Fc`、含 salt 與 master key 的 `secrets.env`、salt 指紋，以及 `litellm.env` 與
`config.yaml` 的副本。**請把它複製到這台主機以外**：沒有 salt key，傾印檔裡的供應商憑證就永遠是密文。
若備份的 salt 指紋與本機不同，`restore.sh` 會拒絕還原，除非加上 `--with-secrets`。

## 解除安裝

```bash
scripts/uninstall.sh                    # 停止並移除單元；保留資料庫、secret、映像
scripts/uninstall.sh --purge            # 另外刪除 volume、network 與 secret，並先做最後一次備份
scripts/uninstall.sh --purge-images     # 另外移除兩個固定版本映像（前提是沒有別的東西在用）
```

`--purge` 是這些腳本刪除資料的唯一方式；它會要求輸入應用名稱確認（`--yes` 可略過）。
env 檔會留在 `~/.config/litellm/`，請自行刪除。

## 對外存取

本 repo 不會把 gateway 發布到網際網路。請「只選一種」：

- 在 `127.0.0.1:4000` 前面放反向代理（nginx 或 Caddy，務必關閉回應緩衝，串流才會正常），由它終結
  TLS 並加上自己的驗證；
- 使用 Tailscale 或 WireGuard 疊加網路，搭配 `scripts/install.sh --bind <疊加網路 IP>`；
- 有防火牆保護的區網連接埠（`--bind <區網 IP>`），並接受該區網上任何持有金鑰者都能連到管理介面。

**絕對不要重複使用 k3s 的 Cloudflare tunnel token。** 用同一組 token 註冊的第二個 connector 會成為
同一條 tunnel 的另一個端點，Cloudflare 會把正式流量在兩者之間做負載平衡。請改建立新的 tunnel、
新的主機名稱與新的 token。

## 從 compose 部署遷移

compose 已經移除（見 [Docker 與 compose](#docker-與-compose)）。若要沿用舊版 `podman-compose`
部署的資料：

```bash
# compose 的容器名稱與 Quadlet 相同
podman exec litellm-postgres pg_dump -U litellm -d litellm --format=custom --no-owner >/tmp/old.dump
podman-compose down                       # 在舊的 checkout 中執行；它的 volume 會保留
mkdir -p ~/old-backup && mv /tmp/old.dump ~/old-backup/litellm-$(date +%Y%m%d-%H%M%S).dump
printf 'LITELLM_SALT_KEY=%s\n' "<舊 .env 中的 salt key>" >~/old-backup/secrets.env
chmod 600 ~/old-backup/*; scripts/install.sh; scripts/restore.sh ~/old-backup --with-secrets
```

舊容器必須先停止並改名（或移除）：Quadlet 單元使用相同的容器名稱，而 `install.sh` 拒絕取代不是
它管理的容器。

## Docker 與 compose

Docker Compose 與 Portainer 已不在本 repo 中：在本機群使用的 podman-compose 1.0.6 上，健康檢查把關
會被無聲丟棄、`restart: unless-stopped` 在開機後不會恢復，而且兩條路徑共用容器名稱，可能把資料庫
清空。最後一個含 `docker-compose.yml` 的 commit 標記為
[`compose-final`](https://github.com/WOOWTECH/Woow_podman_litellm/tree/compose-final)，不再維護。
叢集請用 `Woow_k3s_litellm`。

## 疑難排解

| 症狀 | 原因與處理 |
|---|---|
| `converting "x.container": invalid port format` | 4.9.3 的 `PublishPort=` 不接受 `${VAR}`。本 repo 渲染的是真實值，代表你改了已安裝的單元；重跑 `scripts/install.sh`。 |
| `unsupported key 'X' in group 'Container'` | 你的 podman 不認得該鍵，**整個單元會被跳過**；用 `tests/dryrun.sh` 檢查。 |
| 啟動時 `Error: secret litellm-... not found` | secret 被刪除了。`scripts/install.sh` 會重建缺少的 secret，資料庫密碼除外（見下一列）。 |
| volume 還在但 `litellm-postgres-password` 不見了 | 資料庫密碼未知。若有備份就重建 secret；否則先建立任意值、執行 `scripts/install.sh`（proxy 會連不上），再執行 `scripts/rotate-secrets.sh --db`，它會透過容器內的本機 socket 重設角色密碼。 |
| proxy 不健康，log 顯示 "relation does not exist" | schema 沒被建立。本套件的 `DISABLE_SCHEMA_UPDATE` 必須維持 `false`（只有一個 proxy、沒有 migration job）。 |
| 模型突然失效，log 問 "Did your master_key/salt key change recently?" | salt key 被換掉了。請還原舊值；它永遠不可輪替。 |
| 重開機後 `Job for litellm.service failed` | 檢查 `loginctl show-user "$USER" --property=Linger`（應為 `yes`）。 |

## 目錄結構

```
quadlet/        litellm.container、litellm-postgres.container、litellm.network、
                litellm-pgdata.volume、render-vars（@@VAR@@ 白名單）
config/         config.yaml（與 k3s 共用，CI 固定雜湊）、litellm.env.example
scripts/        install、upgrade、uninstall、backup、restore、rotate-secrets；
                lib/quadlet-lib.sh（自 Woow_quadlet_migration_plan vendored）
tests/          dryrun.sh（vendored）+ dryrun.local.sh + fixtures/、smoke.sh
docs/           architecture.md、k3s-to-podman.md
DEPLOYMENT.md   長篇部署指南（先決條件、secret、day-2、rootful 附錄）
SKILL.md        給 agent 用的簡短 runbook
```

## 授權

MIT，見 [LICENSE](LICENSE)。
