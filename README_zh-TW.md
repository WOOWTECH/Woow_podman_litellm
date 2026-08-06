# Podman 上的 LiteLLM Gateway（搭配 PostgreSQL 16）

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Podman](https://img.shields.io/badge/Podman-4.4%2B%20%7C%205.0%2B%20recommended-892CA0?logo=podman&logoColor=white)](https://podman.io/)
[![LiteLLM](https://img.shields.io/badge/LiteLLM-v1.83.14--stable-00A67E)](https://github.com/BerriAI/litellm)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16--alpine-336791?logo=postgresql&logoColor=white)](https://www.postgresql.org/)

[English](README.md) | [繁體中文](#概觀)

---

## 概觀

一套與 OpenAI 相容的 **LiteLLM proxy**，後端搭配 **PostgreSQL 16**，在單一 Linux 主機上以
**Podman** 執行：兩個容器、一個橋接網路（bridge network）、一個具名資料卷（named volume）、一個唯讀
設定檔、一個對外發布的連接埠。它把五個由 OpenRouter 提供的模型收攏在單一 `/v1` API 之後，簽發帶有
預算上限與模型允許清單的虛擬金鑰（virtual key），並把金鑰、團隊、使用者、花費與加密後的憑證持久化在
PostgreSQL 中，同時在 `/ui` 提供管理介面（Admin UI）。請從兩條路徑中**擇一**：

- **路徑 A — `docker-compose.yml`**，由 `podman-compose`（或 Portainer）驅動。門檻最低，與其他
  WOOWTECH compose 系列儲存庫一致，可直接從 Git URL 部署。
- **路徑 B — `quadlet/` systemd 單元**，rootless（免 root）。由 systemd 監管容器，開機自動啟動，
  啟動過程由健康檢查把關。這是正式環境路徑。

> **本專案的任何內容都尚未在實際的 Podman 主機上執行過** —— 這裡記載的是有文件依據的行為，不是實測
> 結果。在 `scripts/smoke-test.sh` 印出 `RESULT: 7/7 checks passed.` 之前，請一律視該次部署為未經
> 驗證。切勿在同一台主機上同時執行兩條路徑 —— 它們會綁定相同的連接埠。

### 為什麼選 Podman，以及它如何對應 k3s 部署

這才是本儲存庫真正的重點。同一套 gateway 已經跑在 k3s 上；本套件是刻意為單節點所做的轉譯 —— 不是
移植，也不是複製。[**`docs/k3s-to-podman.md`**](docs/k3s-to-podman.md) 就是這份分析：三種候選的
Podman 方案，以及為什麼**不**提供 `podman kube play`；k3s manifest 中每一個 Kubernetes 物件逐一
對應的構造映射；七件無法轉譯的事（滾動更新、狀態調和 reconciliation、真正的 secret 儲存、三探針
模型、排程器的資源語意、動態佈建、Service 物件）；刻意保留的差異；以及什麼情況下應該留在 Kubernetes。
一句話版本：*如果「這台機器掛掉時會怎樣？」這個問題的答案不能只是「服務就停到它回來為止」，那你需要的
是 Kubernetes。*

## 功能特性

| 功能 | 在此如何實現 |
|---|---|
| 與 OpenAI 相容的 gateway | LiteLLM `v1.83.14-stable`，`/v1/*` 位於連接埠 4000 |
| 五個模型走同一個上游 | OpenRouter slug 宣告於 `config/config.yaml` |
| 虛擬金鑰、預算、團隊、花費 | PostgreSQL 16，`store_model_in_db: true` |
| 管理介面 | `/ui`，使用者 `admin`，密碼 = `LITELLM_MASTER_KEY` |
| 兩條部署路徑 | 根目錄 `docker-compose.yml` **以及** rootless 的 `quadlet/` 單元 |
| 由健康檢查把關的啟動流程 | compose 用 `condition: service_healthy`；Quadlet 用 `Notify=healthy` 加上一個 oneshot 等待單元 |
| 開機自動啟動、rootless | systemd `[Install]` + `loginctl enable-linger`；路徑 B 安裝到 `~/.config/`，不需 root daemon |
| 設定檔只有一份 | 直接掛載真正的 `config/config.yaml` —— 沒有需要手動同步的 ConfigMap 副本 |
| 機密與資料庫不暴露在主機表面 | `.env` / `~/.config/litellm/litellm.env` 權限 `0600` 且已被 git 忽略；Postgres 沒有 `ports:` 也沒有 `PublishPort=` |
| 驗證、備份、還原 | `scripts/smoke-test.sh`（7 項檢查，只有 7/7 才 exit 0）、`scripts/backup.sh`、帶有 salt key 指紋比對關卡的 `scripts/restore.sh` |
| 刻意不提供 ingress | 沒有 cloudflared、沒有 tunnel token —— 對外存取只以文件形式說明 |

## 架構

```mermaid
flowchart TB
    client["API clients (OpenAI-compatible SDKs)"]
    subgraph host["Podman host"]
        pub["Published port 4000<br/>compose 0.0.0.0 / quadlet 127.0.0.1"]
        cfg["config.yaml read-only + env file mode 0600"]
        vol[("Named volume pgdata / litellm-pgdata")]
        subgraph net["Bridge network litellm-network / litellm-net"]
            proxy["litellm v1.83.14-stable :4000"]
            db["litellm-postgres 16-alpine :5432, no host port"]
        end
    end
    client -->|"HTTP + virtual key"| pub --> proxy
    cfg -.->|"/app/config.yaml ro,Z + env"| proxy
    cfg -.->|"POSTGRES_PASSWORD"| db
    proxy -->|"5432 over container-name DNS"| db --- vol
    proxy ==>|"outbound HTTPS 443"| orouter["OpenRouter openrouter.ai/api/v1"]
```

同一張圖的非 mermaid 版本：
```
     API clients  --(HTTP + virtual key sk-...)-->  published port 4000
 PODMAN HOST ========================================================
 |  compose: 0.0.0.0:4000        quadlet: 127.0.0.1:4000            |
 |  +---|-------------------------------------------------------+  |
 |  | BRIDGE NET   compose litellm-network / quadlet litellm-net |  |
 |  |  [ litellm ] ghcr.io/berriai/litellm:v1.83.14-stable :4000 |<-- config.yaml, ro
 |  |   |  postgresql -> litellm-postgres:5432 (aardvark DNS)    |   /app/config.yaml
 |  |  [ litellm-postgres ] postgres:16-alpine  *NO HOST PORT*   |  |
 |  +---|-------------------------------------------------------+  |
 |      v named volume pgdata (A) / litellm-pgdata (B), PGDATA=.../pgdata
 ======================|============================================
      outbound HTTPS 443 only -> https://openrouter.ai/api/v1
```

完整圖表 —— 啟動時序、請求路徑、Quadlet 單元名稱的推導規則、元件參考、資料流、安全邊界 —— 都在
[**`docs/architecture.md`**](docs/architecture.md)（僅有英文版）。

## 服務細節

| | `litellm` | `litellm-postgres` |
|---|---|---|
| 映像檔 | `ghcr.io/berriai/litellm:v1.83.14-stable` | `docker.io/library/postgres:16-alpine` |
| 角色 | 與 OpenAI 相容的 proxy，`/ui` 提供管理介面 | 金鑰、團隊、使用者、花費、加密後的憑證 |
| 連接埠 4000 / 5432，是否對外發布 | `${LITELLM_PORT:-4000}:4000`（A）/ `127.0.0.1:4000:4000`（B） | **絕不發布** |
| 啟動參數 | `--config /app/config.yaml --port 4000` | 映像檔原本的 entrypoint，未修改 |
| 設定 / 資料 | `config/config.yaml` 以 `ro,Z` 掛載於 `/app/config.yaml` | 具名資料卷掛載於 `/var/lib/postgresql/data`，`PGDATA=.../pgdata` |
| 健康檢查探針 | 以 `python -c` 的 urllib 探測 `/health/liveliness`（20s/10s/6，start 120s） | `pg_isready -U litellm -d litellm`（10s/5s，start 10s） |
| 啟動探針（startup probe） | 僅路徑 B：`/health/readiness`，40 × 15s | — |
| 資源上限 | 2 GiB 記憶體、2.0 CPU | 1 GiB 記憶體、1.0 CPU |
| 重新啟動策略 | `unless-stopped`（A）/ `Restart=always`、`RestartSec=10`（B） | 同左 |

`config/config.yaml` 中的模型全部經由 `https://openrouter.ai/api/v1` 轉送：`gpt-4o-mini`
→ `openrouter/openai/gpt-4o-mini`、`glm-4.6` → `openrouter/z-ai/glm-4.6`、`minimax-m2` →
`openrouter/minimax/minimax-m2`、`claude-sonnet-4.5` → `openrouter/anthropic/claude-sonnet-4.5`、
`llama-3.3-70b` → `openrouter/meta-llama/llama-3.3-70b-instruct`。`litellm_settings` 設定了
`drop_params: true` 與 `request_timeout: 600`；API 金鑰、master key 與資料庫 URL 都以
`os.environ/...` 間接取值，因此 `config/config.yaml` 裡不會寫入任何機密。

## 部署到 Portainer

Portainer **只吃路徑 A** —— 它讀取儲存庫根目錄的 Compose 檔，而 Quadlet 單元屬於 systemd 設定，
無法貼進 stack 裡。*Stacks → Add stack → Repository：*

| 欄位 | 值 |
|---|---|
| Repository URL | `https://github.com/WOOWTECH/Woow_podman_litellm` |
| Repository reference | `refs/heads/main` |
| Compose path | `docker-compose.yml` |
| Authentication | 關閉（公開儲存庫） |

接著在 Portainer 自己的 *Environment variables* 編輯器中加入 [環境變數參考](#環境變數參考) 裡的
變數 —— 儲存庫本身不含 `.env`，只有 `.env.example`。若想改用貼上檔案的方式，請抓取 raw URL 後使用
*Add stack → Web editor*：

```bash
curl -sSLO https://raw.githubusercontent.com/WOOWTECH/Woow_podman_litellm/main/docker-compose.yml
```

Web editor 無法一併取得 `config/config.yaml`，所以請自行把該檔案放到主機上 bind mount 所預期的位置
（`./config/config.yaml`，相對於該 stack 的工作目錄）。Portainer 會用 stack 名稱推導專案名稱 ——
這也是本檔案不設頂層 `name:` 鍵的原因。

## 前置需求

| 需求 | 路徑 A（compose） | 路徑 B（Quadlet） |
|---|---|---|
| Podman | 最低 4.6 —— `depends_on: condition: service_healthy` 需要 `podman wait --condition=healthy` | 最低 4.4，**建議 5.0+** —— `Notify=healthy` 需要 5.0+，原生 `Memory=` 需要 5.5+ |
| Compose 提供者 | `podman-compose >= 1.3`，或透過 `podman compose` 使用 Docker Compose v2 | 不需要 |
| systemd | 不需要 | 需要真正的 `systemd --user` 工作階段（`XDG_RUNTIME_DIR` 已設定） |
| cgroups / 記憶體 / 磁碟 | v2 / 4 GiB / 約 10 GiB 可用空間 / 2 核心 | 同左 |
| 網路 | 可對外 HTTPS 連到 `ghcr.io`、`docker.io`、`openrouter.ai` | 同左 |
| 帳號 | 來自 <https://openrouter.ai/keys> 的 OpenRouter API 金鑰（`sk-or-...`） | 同左 |

```bash
podman --version && podman info --format '{{.Host.CgroupsVersion}}'   # 預期輸出：v2
podman-compose --version            # 路徑 A
systemctl --user is-system-running  # 路徑 B
echo "sk-$(openssl rand -hex 32)"   # -> LITELLM_MASTER_KEY
echo "sk-$(openssl rand -hex 32)"   # -> LITELLM_SALT_KEY（務必另外產生）
openssl rand -hex 24                # -> POSTGRES_PASSWORD（十六進位可避開 $ # ' " ）
```

請在目標主機上產生這些機密，且絕不重複使用。此外，rootless 主機預設不會把 `cpu`/`cpuset` 控制器
委派給使用者 slice，因此在你加入下列 drop-in 之前，`--cpus` 可能會被忽略：

```bash
sudo mkdir -p /etc/systemd/system/user@.service.d
sudo tee /etc/systemd/system/user@.service.d/delegate.conf >/dev/null <<'EOF'
[Service]
Delegate=memory pids cpu cpuset
EOF
sudo systemctl daemon-reload    # 接著登出「所有」工作階段再重新登入
```

## 快速開始

擇一路徑即可。包含 rootful 安裝在內的完整逐步說明，請見
[**`DEPLOYMENT_zh-TW.md`**](DEPLOYMENT_zh-TW.md)。

### 路徑 A —— podman-compose（最快）

1. **Clone 並建立 `.env`。**
   ```bash
   git clone https://github.com/WOOWTECH/Woow_podman_litellm.git && cd Woow_podman_litellm
   cp .env.example .env && chmod 600 .env
   ```
2. **填入實際值**，執行 `${EDITOR:-vi} .env` —— 取代 `sk-or-REPLACE_ME`、兩個
   `sk-REPLACE_ME` 佔位字串與 `CHANGE_ME_TO_SECURE_PASSWORD`，並把同一組密碼也填進
   `DATABASE_URL`。
3. **先驗證**：
   `export COMPOSE_PROJECT_NAME=litellm && podman-compose config >/dev/null`。
   `${VAR:?message}` 這類守衛會讓缺值時大聲失敗，而不是啟動一個壞掉的容器。
   **請務必保留 `>/dev/null`。** `config` 會把合併後的檔案連同**代入後**的每一個變數
   一起輸出，所以未重導向的 stdout 會以明文印出 `OPENROUTER_API_KEY`、
   `LITELLM_MASTER_KEY`、`LITELLM_SALT_KEY`、`POSTGRES_PASSWORD`，以及內含密碼的
   `DATABASE_URL` —— 進到終端機捲動紀錄、進到 `tee`／CI 記錄，也會進到你之後貼上
   issue 或聊天視窗的內容裡。你真正需要看的守衛訊息走 stderr，仍然會顯示。
   若確實需要檢視算繪結果，請重導向到一個 `chmod 600` 的檔案，用完立刻刪除。
4. **啟動，然後觀察第一次開機。** 預期會有一段時間顯示 `(health: starting)`：先拉映像檔，
   接著跑 Prisma migration，最後還有健康檢查的 120 秒 `start_period`。
   ```bash
   podman-compose up -d && podman-compose ps
   podman logs -f litellm
   ```
5. **驗證**：`./scripts/smoke-test.sh --mode compose` —— 必須印出 `7/7`。

### 路徑 B —— Quadlet + systemd（機器必須能撐過重新開機時的建議做法）

1. **Clone 儲存庫。** 所有 `*.sh` 在版控中的權限都是 `755`，因此 `git clone` 下來即可直接執行。
   若你是下載 zip/tarball，權限位元會遺失，所以下面的 `chmod` 是無害的保險動作。
   ```bash
   git clone https://github.com/WOOWTECH/Woow_podman_litellm.git && cd Woow_podman_litellm
   chmod +x quadlet/*.sh scripts/*.sh
   ```
2. **在 git 工作樹之外建立環境變數檔。** 這個檔案由 systemd 解析，不是 shell：只接受單純的
   `KEY=VALUE` —— 不可有 `${VAR}`、`$(...)`、`export`，也不可有行尾註解。
   ```bash
   mkdir -p ~/.config/litellm && chmod 700 ~/.config/litellm
   cp .env.quadlet.example ~/.config/litellm/litellm.env
   chmod 600 ~/.config/litellm/litellm.env && ${EDITOR:-vi} ~/.config/litellm/litellm.env
   ```
3. **一律先做安裝前的 dry-run：** `./quadlet/install.sh --dry-run`
   （只檢查 unit 檔語法，不會讀取 `--env-file`；環境變數檔由步驟 4 驗證）
4. **安裝。** 它會預檢 Podman 與 cgroup v2、驗證環境變數檔、複製單元檔、執行產生器（generator）
   的 dry-run、預先拉取映像檔、啟用 linger，然後 reload 並啟動。可用旗標為 `--no-pull`、
   `--no-linger`、`-h`。結束碼 `0` = 健康；`1` = 部分成功，請閱讀除錯區塊。
   ```bash
   ./quadlet/install.sh --env-file ~/.config/litellm/litellm.env
   ```
5. **確認單元。** 單元名稱由*檔名*推導而來，因此你會得到 `litellm.service`、
   `litellm-postgres.service`、`litellm-network.service` 與 `litellm-pgdata-volume.service`。
   若產生器的 dry-run 結果與本儲存庫中任何名稱不一致，以 dry-run 為準。
   ```bash
   /usr/lib/systemd/system-generators/podman-system-generator --user --dryrun
   systemctl --user list-units 'litellm*'
   loginctl show-user "$USER" --property=Linger     # 預期 Linger=yes
   ```
6. **驗證**：`./scripts/smoke-test.sh --mode quadlet` —— 必須印出 `7/7`。

## 安裝後設定

**管理介面：** `http://127.0.0.1:4000/ui` —— 使用者 `admin`，密碼為 `LITELLM_MASTER_KEY`。
請簽發範圍受限的虛擬金鑰，而不是把 master key 直接發出去；並實際送出一次真正的 completion，
以證明整條路徑可用（成本不到一分錢的零頭）：

```bash
H=(-H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json')
curl -sS http://127.0.0.1:4000/key/generate "${H[@]}" \
  -d '{"models":["gpt-4o-mini"],"max_budget":5,"key_alias":"demo"}'
curl -sS http://127.0.0.1:4000/v1/chat/completions "${H[@]}" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"ping"}]}'
```

**新增或變更模型**：編輯 `config/config.yaml`，然後只重新啟動 proxy：

```bash
${EDITOR:-vi} config/config.yaml
podman-compose restart litellm                                     # 路徑 A —— bind mount
install -m 0644 config/config.yaml ~/.config/litellm/config.yaml   # 路徑 B —— 安裝後的副本
systemctl --user restart litellm.service                           # 路徑 B
```

新增 slug 前，請先對照線上目錄驗證（`curl -s https://openrouter.ai/api/v1/models`）——
OpenRouter 會下架 slug，先前那筆沒有版本後綴的 `anthropic/claude-3.5-sonnet` 就是這樣消失的。
另請注意：來自檔案與來自資料庫的 `model_list` 條目是**合併**的，不是取代：在 `config.yaml` 中
定義的模型無法從管理介面刪除。

**對外存取只以文件形式說明。** 本專案不會把 gateway 發布到網際網路。`DEPLOYMENT_zh-TW.md` 第 8 節
說明三種選項，而你只該挑其中一種：在 `127.0.0.1:4000` 前面放一層反向代理（nginx 或 Caddy，並關閉
buffering 以免破壞串流）、Tailscale 或 WireGuard 疊加網路，或是有防火牆保護的區域網路連接埠。

## 常用指令

| 工作 | 路徑 A（compose） | 路徑 B（Quadlet） |
|---|---|---|
| 檢視狀態 | `podman-compose ps` | `systemctl --user list-units 'litellm*'` |
| 追蹤 proxy 日誌 | `podman-compose logs -f litellm` | `journalctl --user -u litellm.service -f` |
| 追蹤資料庫日誌 | `podman-compose logs -f postgres` | `journalctl --user -u litellm-postgres.service -f` |
| 重新啟動 proxy | `podman-compose restart litellm` | `systemctl --user restart litellm.service` |
| 停止（保留資料） | `podman-compose down` | `systemctl --user stop litellm.service litellm-postgres.service` |
| 再次啟動 | `podman-compose up -d` | `systemctl --user start litellm-postgres.service litellm.service` |
| 解除安裝（保留資料） | `podman-compose down` 後刪除本專案目錄 | `./quadlet/uninstall.sh`（會移除 unit 檔；要復原請重跑 `install.sh`） |
| 套用單元檔／設定檔的修改 | `podman-compose up -d` | 先 `systemctl --user daemon-reload` 再重新啟動 |

```bash
podman inspect --format '{{.State.Health.Status}}' litellm litellm-postgres
podman exec -it litellm-postgres psql -U litellm -d litellm
curl -s http://127.0.0.1:4000/health/liveliness    # 只看行程，不需驗證，不碰資料庫
curl -s http://127.0.0.1:4000/health/readiness     # 會檢查資料庫，資料庫掛掉時回 503
./scripts/smoke-test.sh                            # --mode compose|quadlet|auto
./scripts/backup.sh --out /srv/backups             # -> litellm-backup-<ts>.tar.gz，0600
./scripts/restore.sh /srv/backups/litellm-backup-<ts>.tar.gz
```

> **絕對不要去探測單純的 `/health`。** 它需要金鑰，**而且**會對每一個已設定的模型發出真實請求，
> 每輪輪詢都在燒 OpenRouter 額度 —— 請改用 `/health/liveliness` 或 `/health/readiness`。
> 另外，**LiteLLM 容器內並沒有 `curl`**（該映像檔內建 Python，沒有 curl），這也是為什麼兩個
> 容器內健康檢查都寫成 `python -c "import urllib.request,sys; ..."` 單行指令。從主機端執行
> `curl` 則沒有問題。

## 疑難排解

| 症狀 | 原因 | 解法 |
|---|---|---|
| 第一次開機時 `litellm` 卡在 `(health: starting)` 好幾分鐘 | 正常現象 —— 拉映像檔、跑 Prisma migration，再加上 120 秒的 `start_period` | 一邊等一邊看 `podman logs -f litellm`。要等整段啟動預算跑完（路徑 B：40 × 15 秒）之後，才需要懷疑真的有問題。 |
| 出現 `relation "LiteLLM_..." does not exist`，或完全查不到 `LiteLLM%` 資料表 | 對著空資料庫設了 `DISABLE_SCHEMA_UPDATE=true` —— LiteLLM 只會跑唯讀的 `prisma migrate diff`，什麼也不會建立 | 改回 `false` 並重新啟動 proxy。見 `DEPLOYMENT_zh-TW.md` 第 5 節。 |
| `could not translate host name "litellm-postgres"` | 容器落在 Podman 預設的 `podman` 網路上，而該網路沒有 DNS | 兩條路徑都會建立自訂橋接網路，正是為了這個原因。檢查 `podman network ls`，並確認 `litellm-network.service`（路徑 B）已啟動。 |
| `Unit litellm-postgres.service not found` | Quadlet 因為遇到無法辨識的鍵而**靜默略過**該檔案 —— 最常見的是在 Podman 4.x 上使用 `Notify=healthy` | 執行產生器 dry-run 並閱讀 stderr。在 4.x 上把 `Notify=healthy` 註解掉；`litellm-wait-postgres.service` 仍然會把關就緒狀態。然後 `daemon-reload`。 |
| `Start operation timed out`，或重新開機後整套服務不見了（路徑 B） | 冷啟動拉映像檔超過 `TimeoutStartSec`；或是 linger 沒開，導致 systemd user manager 從未啟動 | 預先拉取兩個映像檔（`install.sh` 會做，除非加了 `--no-pull`）。執行 `loginctl enable-linger "$USER"`，並以 `loginctl show-user "$USER" --property=Linger` 確認。 |
| 連接埠 4000 出現 `bind: address already in use` | 有其他程式佔用該連接埠 | `ss -ltnp \| grep :4000`。把它釋放，或改變 `LITELLM_PORT`（A）/ `PublishPort=`（B）。 |
| proxy 回應 `401 Unauthorized` | 執行中的 proxy 載入的是另一組 `LITELLM_MASTER_KEY` | 用 `podman exec litellm printenv LITELLM_MASTER_KEY` 比對。金鑰只在啟動時讀取 —— 改完環境變數務必重新啟動。 |
| Postgres 資料目錄出現 `permission denied` | 使用了 rootless 的 bind mount；容器內 UID 70 沒有對應到可寫入的主機 UID | 請使用兩條路徑都提供的具名資料卷。絕對不要對 `PGDATA` 使用 bind mount。 |
| `--cpus` 看起來被忽略 | `cpu`/`cpuset` 沒有委派給你的使用者 slice | 加入[前置需求](#前置需求)中的 `delegate.conf` drop-in，然後登出所有工作階段。 |
| 日誌出現 `Did your master_key/salt key change recently?` | `LITELLM_SALT_KEY` 被改過了。解密失敗是**非阻斷式**的，所以不會有任何東西崩潰 | 還原成原本的 salt key。見[安全注意事項](#安全注意事項)。 |

更長的表格與一套通用的診斷流程在 [`DEPLOYMENT_zh-TW.md` 第 10 節](DEPLOYMENT_zh-TW.md)；相同的故障模式以指令式寫法呈現於 [`SKILL.md`](SKILL.md)（僅有英文版）。

## 檔案結構

```
Woow_podman_litellm/
├── README.md                          # 英文版說明（本檔的原文）
├── README_zh-TW.md                    # 繁體中文版（本檔）
├── DEPLOYMENT.md                      # 完整部署指南、日常維運、解除安裝
├── DEPLOYMENT_zh-TW.md                # 繁體中文部署指南（DEPLOYMENT.md 的中文版）
├── SKILL.md                           # 指令式操作手冊（供代理程式使用的 skill）
├── LICENSE                            # MIT
├── .gitignore                         # 阻擋 .env、data/、backups/、*.sql、*.tar.gz 等
├── docker-compose.yml                 # 路徑 A —— 完整堆疊，可用 Portainer 部署
├── .env.example                       # 路徑 A 的環境變數範本（雙語）
├── .env.quadlet.example               # 路徑 B 的環境變數範本 + systemd env-file 語法規則
├── config/
│   └── config.yaml                    # 模型 + general/litellm 設定（用掛載，絕不複製）
├── quadlet/                           # 路徑 B —— rootless systemd 單元
│   ├── litellm.network                # -> litellm-network.service，      網路 litellm-net
│   ├── litellm-pgdata.volume          # -> litellm-pgdata-volume.service，資料卷 litellm-pgdata
│   ├── litellm-postgres.container     # -> litellm-postgres.service
│   ├── litellm.container              # -> litellm.service
│   ├── litellm-wait-postgres.service  # 一般單元（非 Quadlet），Podman 4.x 的排序備援
│   ├── install.sh                     # 預檢、安裝、驗證、拉取、linger、啟動
│   └── uninstall.sh                   # 預設不具破壞性；另有 --purge-* 旗標
├── scripts/
│   ├── smoke-test.sh                  # 7 項檢查；只有 7/7 才 exit 0
│   ├── backup.sh                      # pg_dump + config.yaml + MANIFEST.txt，權限 0600
│   └── restore.sh                     # salt key 指紋關卡、drop/create、pg_restore
├── docs/
│   ├── architecture.md                # 圖表、元件參考、安全邊界
│   └── k3s-to-podman.md               # 為何選 Podman、完整構造對應、被否決的方案
└── .github/workflows/
    └── lint.yml                       # 僅做靜態檢查——絕不啟動任何容器
```

CI 會執行 `bash -n` 與 `shellcheck`、解析兩個 YAML 檔、將 `config/config.yaml` 的雜湊值釘選成與
k3s 副本一致、確認 Quadlet unit 檔保有必要的區段，並拒絕任何長得像憑證的字串。
**CI 顯示綠燈並不代表這套堆疊已成功部署**——本儲存庫中沒有任何內容在真實的 Podman 主機上執行過。
只有 `scripts/smoke-test.sh` 能做出那樣的宣稱。

儲存庫中每一個 `*.sh` 在版控中的權限都是 `755`；若你下載的是 zip/tarball 而非 clone，權限位元
會遺失 —— 請執行 `chmod +x quadlet/*.sh scripts/*.sh` 還原。兩條路徑都把設定檔掛載到
`/app/config.yaml` —— 路徑 A 來自
`./config/config.yaml`，路徑 B 來自安裝後的副本 `~/.config/litellm/config.yaml`。

## 環境變數參考

以下涵蓋 `.env.example` 中的每一個變數。`.env.quadlet.example` 少了 `LITELLM_PORT`
（路徑 B 的對外連接埠直接寫在 `litellm.container` 中），並且把 `STORE_MODEL_IN_DB`、
`LITELLM_MODE`、`LITELLM_LOG`、`DISABLE_SCHEMA_UPDATE` 這四個註解掉——因為在路徑 B 上，
這四個由 unit 檔的 `Environment=` 行寫死，會覆蓋 env 檔：
在路徑 B 上，對外發布的連接埠是寫死在 `litellm.container` 裡的靜態文字。

| 變數 | 預設值 | 必填 | 說明 |
|---|---|---|---|
| `OPENROUTER_API_KEY` | `sk-or-REPLACE_ME` | **是** | 來自 <https://openrouter.ai/keys> 的 OpenRouter 金鑰。由 `config/config.yaml` 以 `os.environ/OPENROUTER_API_KEY` 讀取。 |
| `LITELLM_MASTER_KEY` | `sk-REPLACE_ME` | **是** | 管理用憑證，**同時也是**管理介面中 `admin` 使用者的密碼。必須以 `sk-` 開頭。用 `echo "sk-$(openssl rand -hex 32)"` 產生。 |
| `LITELLM_SALT_KEY` | `sk-REPLACE_ME` | **是** | 用來加密儲存在 PostgreSQL 中的每一組供應商憑證。**設定一次，永不輪替。** 請與 master key 分開產生。 |
| `POSTGRES_USER` | `litellm` | 否 | 資料庫角色。與 k3s 部署一致。若要更改，請同步更新 `DATABASE_URL` 與 Quadlet 的 `HealthCmd=`。 |
| `POSTGRES_PASSWORD` | `CHANGE_ME_TO_SECURE_PASSWORD` | **是** | 資料庫密碼。用 `openssl rand -hex 24` 產生 —— 十六進位可避開 `$`、`#` 與引號跳脫問題。 |
| `POSTGRES_DB` | `litellm` | 否 | 資料庫名稱。注意事項同 `POSTGRES_USER`。 |
| `DATABASE_URL` | `postgresql://litellm:CHANGE_ME_TO_SECURE_PASSWORD@litellm-postgres:5432/litellm` | **是** | 必須內嵌同一組密碼，且主機名稱必須是 `litellm-postgres` —— **不是** `localhost`。密碼中若含 `: / ? # [ ] @` 任一字元，必須做百分號編碼。 |
| `LITELLM_PORT` | `4000` | 否 | 僅路徑 A 使用的主機連接埠。路徑 B 在單元檔中綁定 `127.0.0.1:4000:4000`。 |
| `STORE_MODEL_IN_DB` | `True` | 否 | 讓從管理介面新增的模型持久化到 PostgreSQL。 路徑 B：此值寫死在 `quadlet/litellm.container`，env 檔中的設定會被忽略。 |
| `LITELLM_MODE` | `PRODUCTION` | 否 | 停用 LiteLLM 的 `load_dotenv()`，避免不小心把本機的 `.env` 自動載入。 路徑 B：此值寫死在 `quadlet/litellm.container`，env 檔中的設定會被忽略。 |
| `LITELLM_LOG` | `INFO` | 否 | `DEBUG` / `INFO` / `ERROR`。請注意這裡的命名有一級落差：`INFO` 大致上已經等同 CLI 的 `--debug`。除錯日誌可能包含請求內容。 路徑 B：此值寫死在 `quadlet/litellm.container`，env 檔中的設定會被忽略。 |
| `DISABLE_SCHEMA_UPDATE` | `false` | 否 | **請維持 `false`。** k3s 部署設為 `true`；若把那個值照抄過來，`prisma migrate diff` 只會把 SQL 印出來、對著空的資料卷什麼都不建立，proxy 也就永遠不會變成健康狀態。 路徑 B：此值寫死在 `quadlet/litellm.container`，env 檔中的設定會被忽略。 |

compose 檔以 `${VAR:?message}` 守衛其中五個變數 —— `OPENROUTER_API_KEY`、
`LITELLM_MASTER_KEY`、`LITELLM_SALT_KEY`、`DATABASE_URL`、`POSTGRES_PASSWORD` —— 缺任何一個
都會拒絕啟動。Quadlet 沒有對等的守衛機制 —— 缺值就只是空字串，容器會在稍後才失敗，這也是
`scripts/smoke-test.sh` 存在的理由之一。

## 安全注意事項

**絕對不要把 `.env` commit 進去。** `.gitignore` 已經阻擋 `.env`、`.env.*`（只放行 `*.example`
範本）、`*.env`、`secrets/`、`*.key`、`*.pem`、`data/`、`pgdata/`、`backups/`、`*.sql`、
`*.tar.gz` 之類的檔案。不要加例外，也不要 `git add -f`。請把該檔案維持在 `600` 權限；在路徑 B 上
更要讓它完全待在工作樹之外，放在 `~/.config/litellm/`（`700`）。

> ### ⚠ `LITELLM_SALT_KEY` —— 設定一次，永遠不要輪替
>
> 它負責加密 LiteLLM 存放在 PostgreSQL 中的每一組供應商憑證。**只要改動它，所有這些憑證都會
> 永久無法解密** —— 除了清空資料庫並手動重新輸入每一組憑證之外別無他法。更糟的是，這個失敗是
> 靜默的：解密錯誤屬於非阻斷式，所以 LiteLLM 只會記下 *"Did your master_key/salt key change
> recently?"*、回傳 `None`，然後繼續提供服務。若完全不設定，它會退回使用 `LITELLM_MASTER_KEY`
> 當作 salt，這會讓輪替 master key 同樣具有破壞性。請在第一次啟動之前就明確且分別設定這兩個值，
> 並把 salt key 備份到主機之外；`scripts/backup.sh` 只會存放截短過的 SHA-256 **指紋**，因此
> `scripts/restore.sh` 會拒絕還原到指紋不符的堆疊上。

**兩條路徑都不會對外發布 Postgres 的連接埠** —— compose 服務沒有 `ports:`，
`litellm-postgres.container` 也沒有 `PublishPort=`。這個資料庫存放了每一個虛擬金鑰的雜湊、每一筆
預算資料與加密後的憑證，而主機上沒有任何東西需要這個連接埠；臨時要存取時請用
`podman exec -it litellm-postgres psql -U litellm -d litellm`。

**如果你在正式環境偏好 `podman secret`，也可以改用它。** 兩條路徑刻意採用單純的 `0600` 環境變數檔，
因為 Podman 預設的 secret driver 是把機密未加密地存在使用者的資料目錄下 —— 那只是把問題搬家，
並沒有解決問題。Quadlet 可透過 `Secret=` 提供機密。

**輪替 `LITELLM_MASTER_KEY`** 只有在 `LITELLM_SALT_KEY` 有明確設定的前提下才安全：更新
`.env` / `litellm.env` 中的值、重新啟動 proxy（`podman-compose up -d --force-recreate litellm`，
或 `systemctl --user restart litellm.service`），並重新發布管理介面的密碼。既有的虛擬金鑰不受影響。
**但要注意：** 如果 `LITELLM_SALT_KEY` 從未設定過，master key *就是* salt，輪替它會靜默地摧毀所有
已儲存的憑證。更好的做法是根本不要把 master key 發出去：改為簽發帶 `max_budget` 與 `models`
允許清單的受限金鑰。

> ### ⚠ 本套件刻意不提供 Cloudflare tunnel
>
> 本儲存庫中沒有 `cloudflared` 容器、沒有 tunnel 單元、也沒有任何 `TUNNEL_TOKEN` 變數，這是刻意
> 的設計。tunnel token 識別的是一條 **tunnel**，而不是一個 connector：用**同一組** token 啟動
> 第二個 `cloudflared`，等於在同一條 tunnel 上再註冊一個來源，Cloudflare 會在兩者之間做負載平衡
> —— 也就是把無法預期比例的線上請求送進這套堆疊，而它有著不同的資料庫、不同的虛擬金鑰與不同的花費
> 紀錄。**現行 k3s 部署所使用的那組 token 此刻正在使用中。絕對不要把它貼到這裡。** 如果你確實需要
> tunnel，請建立一條**全新的** tunnel，配上它自己的 token 與主機名稱，並在本儲存庫之外自行執行
> `cloudflared`。

## 更新

兩個映像檔都已釘選版本，而且不會自動更新：`AutoUpdate=registry` 是刻意不設定的，因為在資料目錄
仍然掛載使用中的情況下無人值守地升級資料庫引擎，正是你最不想遇到的事。**請先做備份** ——
較新版的 proxy 可能會在啟動時執行 migration。

```bash
./scripts/backup.sh --out /srv/backups
# 路徑 A
${EDITOR:-vi} docker-compose.yml          # 修改映像檔標籤
podman-compose pull && podman-compose up -d && ./scripts/smoke-test.sh --mode compose
# 路徑 B
${EDITOR:-vi} quadlet/litellm.container   # 修改 Image=
podman pull ghcr.io/berriai/litellm:NEW_TAG   # 剛才設定的新標籤，不是舊的
./quadlet/install.sh --env-file ~/.config/litellm/litellm.env   # 複製、reload、重新啟動
./scripts/smoke-test.sh --mode quadlet
```

回滾的程序完全相同，只是換成前一個標籤；若先前跑過 migration，還要再加上一次還原。若想改用 digest
釘選，兩個 `.container` 單元都附有一行註解起來的 `Image=...@sha256:PASTE_DIGEST_HERE`；
`podman image inspect <img> --format '{{index .RepoDigests 0}}'` 可以印出 digest。本儲存庫沒有
附上任何 digest，因為沒有實際拉取或驗證過。

## 授權

[MIT](LICENSE) © 2026 WOOWTECH。

## 其他部署平台

- **K3s / Docker Compose** —— [`WOOWTECH/Woow_litellm_docker_compose`](https://github.com/WOOWTECH/Woow_litellm_docker_compose)。
  本儲存庫的源頭，也是*應用層*的真實來源 —— 模型清單、環境變數契約、映像檔標籤。它的 k3s manifest
  執行著現行的正式環境 gateway；本儲存庫中沒有任何東西會管理、變更或指向那套部署。
- **MCP 管理主控台** —— [`WOOWTECH/Woow_litellm_mcp_server`](https://github.com/WOOWTECH/Woow_litellm_mcp_server)。
  從 MCP 用戶端管理執行中 gateway 的金鑰、團隊、使用者與花費。
