# 部署指南

[`README.md`](README_zh-TW.md) 的長篇補充。本文只涵蓋一種部署形態：**rootless Podman + Quadlet +
`systemd --user`**，由 `scripts/install.sh` 驅動。compose 已移除（見 README 的「Docker 與 compose」）。

[English](DEPLOYMENT.md) · **繁體中文**

> **尚未在真實 Podman 主機上執行過。** 單元以 podman 4.9.3 的 Quadlet 產生器與
> `systemd-analyze --user verify` 驗證，腳本以 shellcheck 與測試替身驗證。在 `tests/smoke.sh`
> 於該主機上完全通過之前，請把任何部署視為未驗證。

---

## 1. 先決條件

### 1.1 Podman

```bash
podman --version                                    # 4.9 以上；本套件在 4.9.3 上測試
podman info --format '{{.Host.CgroupsVersion}}'     # v2
```

`scripts/install.sh` 低於 4.9 會拒絕執行。Quadlet 本身從 4.4 就有，但本 repo 使用 `PodmanArgs=`
（4.6+），而且只針對 4.9.3 附帶的產生器做過驗證。

### 1.2 podman 4.9.3 做得到與做不到的事

以下四點決定了單元的寫法；第二、三點在本 repo 舊版文件中是錯的。

| 事實 | 對本套件的影響 |
|---|---|
| `Notify=healthy` **會被接受但忽略**（產生的單元是 `--sdnotify=conmon`） | 完全不使用它。把關改用資料庫單元的 `ExecStartPost=`（第 4 節）。 |
| `PublishPort=` 不接受 `${VAR}`（`invalid port format`） | 發布位址與連接埠在安裝時由 env 檔渲染進單元（`@@VAR@@` 標記）。 |
| drop-in 目錄（`x.container.d/*.conf`）**不會生效** | 無法用分層方式覆寫每台主機的值；同樣由渲染解決。 |
| `Memory=` 需要 5.5+、`StopTimeout=` 只有 5.x、沒有 `.pod`/`.build` | 資源上限走 `PodmanArgs=--memory/--cpus`；不使用 `StopTimeout=`。 |

只要出現產生器不認得的鍵，**整個檔案會被跳過**（完全不產生 `.service`），這也是
`tests/dryrun.sh` 要跑真正的產生器、並確認每個來源檔都產生了單元的原因。

### 1.3 真正的 `systemd --user` 工作階段

```bash
systemctl --user is-system-running        # 不能是 "offline"；"degraded" 沒關係
echo "$XDG_RUNTIME_DIR"                   # /run/user/<uid>
```

`su`／`sudo -u` 不會給你這個環境：請以部署使用者透過 SSH 或主控台登入。
`scripts/install.sh` 會幫你啟用 `loginctl enable-linger`（若它自己做不到，需要具備 polkit 權限的
管理者執行一次）；沒有 linger，單元會在登出時停止，而且開機不會啟動。

### 1.4 主機資源

4 GiB 記憶體（proxy 上限 2 GiB、Postgres 1 GiB）、約 10 GiB 可用磁碟（LiteLLM 映像 1.5–2 GiB）、
2 核心，以及可連到 `ghcr.io`、`docker.io`、`openrouter.ai` 的對外 HTTPS。

rootless 主機不會把 `cpu`/`cpuset` controller 委派給 user slice，因此 `--cpus` 可能無聲無效；
修正方式見 README 的「先決條件」。`--memory` 則預設就能運作。

### 1.5 OpenRouter 帳號

一組 API 金鑰（`sk-or-...`），申請處 <https://openrouter.ai/keys>。這是你唯一需要提供的憑證，
其餘都是自動產生的。

---

## 2. Secret：有哪些、放在哪裡

本 repo、單元檔與 journal 中沒有任何憑證。`scripts/install.sh` 會建立五個 podman secret：

| Secret | 值 | 以什麼形式進入容器 |
|---|---|---|
| `litellm-postgres-password` | 32 個隨機英數字 | **檔案** `/run/secrets/postgres-password`，由 `POSTGRES_PASSWORD_FILE` 讀取 |
| `litellm-database-url` | `postgresql://litellm:<密碼>@litellm-postgres:5432/litellm` | `DATABASE_URL`（env secret），每次安裝重新推導 |
| `litellm-master-key` | `sk-` + 48 個隨機英數字，或你的 `LITELLM_MASTER_KEY` | `LITELLM_MASTER_KEY`（env secret） |
| `litellm-salt-key` | `sk-` + 48 個隨機英數字，或你的 `LITELLM_SALT_KEY` | `LITELLM_SALT_KEY`（env secret） |
| `litellm-openrouter-api-key` | 你的 `OPENROUTER_API_KEY` | `OPENROUTER_API_KEY`（env secret） |

你提供的值會從 `~/.config/litellm/litellm.env`（權限 0600）進入 secret，從不會被渲染進單元。
第一次安裝成功後，你可以把該檔中的值清空，secret 會留著。`config/config.yaml` 從不含機密：
它以 `os.environ/OPENROUTER_API_KEY`、`os.environ/LITELLM_MASTER_KEY`、`os.environ/DATABASE_URL`
取值，所以同一個檔案在 k3s 與這裡都能用。

**兩條沒有替代方案的規則。**

1. `LITELLM_SALT_KEY` 加密 LiteLLM 存進 Postgres 的所有供應商憑證。替換它不會重新加密任何東西：
   那些資料列會永久無法解密，而且 LiteLLM 不會大聲失敗（解密錯誤屬非阻斷式，模型只會安靜失效，
   log 裡問一句 *"Did your master_key/salt key change recently?"*）。`install.sh` 會拒絕與現有
   secret 不同的值；`rotate-secrets.sh --salt` 直接拒絕；`backup.sh` 會把它寫進 `secrets.env`，
   讓你在主機之外保存一份。
2. 在 podman 4.9.3 上，env secret **會** 出現在「執行中」容器的 `podman inspect`。因此那四個
   env secret 對任何能使用這個使用者 podman socket 的東西都是可讀的（包含具有 inspect 工具的
   MCP server）。但它們仍然不在 git、單元檔、`systemctl --user cat`、建立指令與 journal 中。
   只有資料庫密碼能避開這點，因為 Postgres 支援 `*_FILE`。

---

## 3. 安裝

```bash
git clone https://github.com/WOOWTECH/Woow_podman_litellm.git
cd Woow_podman_litellm
scripts/install.sh                                  # 建立 env 檔後停下：還沒有金鑰
${EDITOR:-vi} ~/.config/litellm/litellm.env         # 設定 OPENROUTER_API_KEY
scripts/install.sh --port 4000                      # 另外兩個旋鈕是 --bind 與 --set
```

### 3.1 它的執行順序

1. **前置檢查**：不是 root、podman >= 4.9、Quadlet 產生器與 `podman-user-generator` 連結存在、
   `systemctl --user` 有回應、cgroup v2；啟用 linger；取得每個應用的鎖。
2. **設定**：需要時由範例建立 0600 的 env 檔，並把 `--port/--bind/--set` 套用在一份私有副本上。
3. **值的驗證**：bind 必須是 IPv4（非 loopback 會警告）、port 必須是合法 TCP 埠、log level 必須是
   五個之一、master key 必須以 `sk-` 開頭。若 `pgdata` volume 存在卻少了密碼 secret 或 salt secret，
   就中止。
4. **防護**：若存在不受 Quadlet 管理、名為 `litellm` 或 `litellm-postgres` 的容器，中止並提供
   `podman rename` 指令以保留它供回復（Quadlet 以 `podman run --replace` 啟動容器，會把它刪掉）。
   連接埠已被占用也會中止。
5. **渲染與驗證**：單元由 env 檔渲染到暫存目錄，並以 `quadlet -dryrun -user` 加
   `systemd-analyze --user verify` 檢查，同時檢查 `~/.config/systemd/user` 是否有同名檔案遮蔽。
   到這一步為止還沒有寫入任何東西。
6. **儲存**：到這裡才把新的 `--port/--bind/--set` 值寫入 env 檔。
7. **映像**：在任何單元變更之前先拉取兩個固定版本的映像，讓緩慢的拉取不會算進 `TimeoutStartSec`。
8. **Secret**：建立缺少的項目，並重新推導 `DATABASE_URL`。
9. **安裝與套用**：只寫入位元組有變的檔案（原子寫入，若本機有修改會先備份），更新 manifest，
   執行 `daemon-reload`，只重啟檔案有變的單元。沒有變更的重跑不會重啟任何東西。
10. **健康檢查與 smoke**：等待 `litellm-postgres` healthy（最多 5 分鐘），再等 `litellm`
    （最多 15 分鐘：第一次開機要跑 Prisma migration），再等 `/health/readiness`，最後執行
    `tests/smoke.sh`。

`--dry-run` 在第 6 步前停止且不寫入任何東西（沒有 env 檔也能跑：它渲染範例）。`--no-pull` 禁止拉取。
`--no-start` 只安裝檔案就結束。

### 3.2 檔案落點

```
~/.config/containers/systemd/   litellm.container  litellm-postgres.container
                                litellm.network    litellm-pgdata.volume
~/.config/litellm/              litellm.env（0600） config.yaml（0644）
~/.local/state/woow-quadlet/litellm/   manifest、pending-restart、secrets、rollback/
~/backups/litellm/<時間戳>/      備份（目錄 0700、檔案 0600）
```

### 3.3 產生的單元名稱

Quadlet 由**檔名**推導單元名稱，而不是 `ContainerName=`：

| 檔案 | 單元 | 資源 |
|---|---|---|
| `litellm.container` | `litellm.service` | 容器 `litellm` |
| `litellm-postgres.container` | `litellm-postgres.service` | 容器 `litellm-postgres` |
| `litellm.network` | `litellm-network.service` | 網路 `litellm-net`（來自 `NetworkName=`） |
| `litellm-pgdata.volume` | `litellm-pgdata-volume.service` | volume `litellm-pgdata`（來自 `VolumeName=`） |

`Network=litellm.network` 與 `Volume=litellm-pgdata.volume:` 參照的是**檔案**，這才會讓 Quadlet
自動注入對那些服務的 `Requires=`/`After=`。在 4.9.3 上，參照一個沒有安裝的 `.volume` 檔會安靜地
變成全新的 `systemd-<名稱>` volume——`tests/dryrun.sh` 會在這種情況失敗。

```bash
systemctl --user list-units 'litellm*'
QUADLET_UNIT_DIRS=~/.config/containers/systemd /usr/libexec/podman/quadlet -dryrun -user | head -40
```

---

## 4. 就緒把關（readiness gate）

`litellm-postgres.container` 結尾是：

```ini
ExecStartPost=/usr/bin/timeout 240 /bin/sh -c 'until podman exec litellm-postgres pg_isready -h 127.0.0.1 -p 5432 -U litellm -d litellm >/dev/null 2>&1; do sleep 2; done'
```

systemd 會讓單元停在 `activating (start-post)` 直到 `ExecStartPost=` 結束，因此
`litellm.service` 的 `Requires=`/`After=litellm-postgres.service` 真的會等到資料庫接受 TCP 連線為止
——這正是 `Notify=healthy` 原本該做、但在 4.9.3 上不會做的事。

* **為什麼用 `-h 127.0.0.1`**：第一次開機初始化時，Postgres entrypoint 會先跑一個「只監聽 unix
  socket」的暫時伺服器。走 socket 的探測會在真正的伺服器還沒起來時就回報 ready。
* **失敗語意**：若 240 秒內沒有就緒，單元失敗，`Restart=always` 會重試；LiteLLM 會保持停止，而不是
  對著不存在的資料庫不斷崩潰重啟。執行期間 Postgres 崩潰不會停掉 LiteLLM（`Requires=` 只傳遞明確的
  stop/restart），Prisma 會自行重連。
* Postgres 的 `HealthStartPeriod=60s` 是為了讓 `HealthOnFailure=kill` 不會在小機器上殺掉一次較慢的
  初次 `initdb`。

---

## 5. 驗證

```bash
tests/smoke.sh
```

它會檢查：兩個單元都 active、兩個容器都 healthy、`/health/liveliness` 與 `/health/readiness` 回
200、帶 master key 的 `/v1/models` 列出 `config/config.yaml` 中的所有模型、未帶金鑰的 `/v1/models`
回 401、監聽位址只有設定的那一個、Postgres 環境中沒有 proxy 的金鑰，以及 `systemctl --user cat`
中沒有明文憑證。手動對應指令：

```bash
podman inspect --format '{{.State.Health.Status}}' litellm litellm-postgres
podman healthcheck run litellm                     # 立刻執行一次檢查，不必等下個週期
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4000/health/readiness
podman exec litellm-postgres psql -U litellm -d litellm -c '\dt' | head
journalctl --user -u litellm.service -u litellm-postgres.service -o short-precise | tail -40
```

一次端到端的 completion 只要不到一分錢，而且是唯一能證明 OpenRouter 金鑰可用的方法：見 README 的
「第一批請求」。

**兩件不要做的事**：不要探測純 `/health`（它每次輪詢都會對所有設定的模型送出真實請求），
也不要在 LiteLLM 容器內用 `curl` 做健康檢查（映像只有 Python，沒有 curl）。

---

## 6. Day-2 維運

| 工作 | 指令 |
|---|---|
| 狀態 | `systemctl --user list-units 'litellm*'` |
| 日誌 | `journalctl --user -u litellm.service -f`（可再加 `-u litellm-postgres.service`） |
| 重啟 proxy | `systemctl --user restart litellm.service` |
| 停止（保留資料） | `systemctl --user stop litellm.service litellm-postgres.service` |
| 重新啟動 | `systemctl --user start litellm.service`（會先拉起 Postgres） |
| 改設定 | 編輯 `~/.config/litellm/litellm.env`，再執行 `scripts/install.sh` |
| 改模型清單 | 編輯 `config/config.yaml`，再執行 `scripts/install.sh` |
| 升級 | `git pull && scripts/upgrade.sh` |
| 備份／還原 | `scripts/backup.sh` / `scripts/restore.sh <目錄>` |
| 輪替 | `scripts/rotate-secrets.sh --db` / `--master` |
| 解除安裝 | `scripts/uninstall.sh`（`--purge` 才會刪資料） |

請不要直接編輯已安裝的單元檔：下一次 `scripts/install.sh` 會先備份你的修改再覆蓋它。
要改請改 env 檔或 repo。

### 6.1 升級

兩個映像版本都以 repo 為準。修改單元中的 `Image=`（以及 README 徽章），提交，然後：

```bash
scripts/upgrade.sh
```

它會把已安裝的單元快照到 `~/.local/state/woow-quadlet/litellm/rollback/<時間戳>/`、先做一次
`pg_dump`（LiteLLM 的 Prisma migration 是單向的，且在新版第一次開機時執行）、安裝，然後跑 smoke
測試。失敗時會還原快照、以先前的映像重啟（`install.sh` 從不刪除映像 tag，所以它們還在）、
重跑 smoke 測試，並印出還原升級前傾印檔的 `restore.sh` 指令。

`TimeoutStartSec=600` 足以涵蓋慢機器上的 migration；啟動健康檢查另外給 10 分鐘。

### 6.2 舊映像

`podman images | grep -E 'litellm|postgres'`，確認無誤後 `podman rmi <舊 tag>`。
`scripts/uninstall.sh --purge-images` 只會移除「沒有容器、也沒有其他已安裝 Quadlet 單元在用」的
映像（Postgres 映像與其他 WOOWTECH 堆疊共用）。

### 6.3 資源上限

`PodmanArgs=--memory=2g --cpus=2.0`（proxy）與 `--memory=1g --cpus=1.0`（資料庫）寫在單元裡。
LiteLLM 官方指引把每個 worker 約 4 GiB 當成下限；這裡的 2 GiB 是為了與 k3s 一致。若 proxy 在負載下
被 OOM 殺掉，請在單元中調高後重跑 `scripts/install.sh`。

---

## 7. 對外存取

本 repo 不會把 gateway 發布出去。請選一種：

* **反向代理** 放在 `127.0.0.1:4000` 前面（nginx、Caddy）。務必關閉回應緩衝
  （`proxy_buffering off;`），否則串流回應會一次吐出。TLS 在那裡終結。
* **疊加網路**（Tailscale、WireGuard）：`scripts/install.sh --bind <疊加網路 IP>`。
* **受信任的區網連接埠**：`scripts/install.sh --bind <區網 IP>`，再加上主機防火牆規則。此後該區網上
  的所有人都能連到 `/ui` 與 `/v1`，只靠金鑰保護。

**絕對不要重複使用 k3s 的 Cloudflare tunnel token。** 用同一組 token 的第二個 connector 會成為同一條
tunnel 的另一個端點，Cloudflare 會把正式流量在兩者間負載平衡。請建立新的 tunnel、主機名稱與 token。

---

## 8. Rootless 與 rootful

以上全部是 rootless，也是本套件支援的形態：容器以你的使用者身分執行，socket 與資料都在 `$HOME`
底下，不需要 root daemon。

**Rootful 附錄。** 若你必須以 root 執行（system-wide 單元），請記得 `%h` 在那裡是 `/root`，
因此任何以 `%h` 解析的路徑都不能在兩種模式間通用。單元中只有設定檔掛載與 env 檔使用 `%h`：

```ini
# rootful 版本的 litellm.container
Volume=/etc/litellm/config.yaml:/app/config.yaml:ro,Z
```

把檔案安裝到 `/etc/containers/systemd/`、把 `config.yaml` 放在 `/etc/litellm/`（0644），並以 root
建立那五個 secret（`sudo podman secret create ...`）。`scripts/install.sh` 不支援這種模式：
它刻意拒絕以 root 執行。低於 1024 的連接埠需要 rootful 或調整
`net.ipv4.ip_unprivileged_port_start`；4000 不需要。

---

## 9. 疑難排解

| 症狀 | 原因與處理 |
|---|---|
| `converting "x.container": invalid port format '${P}'` | `PublishPort=` 裡有變數。本 repo 渲染真實值，代表你改了已安裝的單元；重跑 `scripts/install.sh`。 |
| `converting "x.container": unsupported key 'K'` | 你的 podman 不認得該鍵，**整個單元被跳過**；執行 `tests/dryrun.sh`。 |
| 單元啟動時 `Error: secret litellm-... not found` | secret 被刪了。`scripts/install.sh` 會重建除資料庫密碼以外的所有 secret。 |
| volume 還在但密碼 secret 不見了 | 角色密碼未知。先用任意值建立該 secret、執行 `scripts/install.sh`（proxy 會連不上），再執行 `scripts/rotate-secrets.sh --db`，它會透過容器內本機 socket 重設角色密碼。 |
| proxy 一直不健康，log 出現 `relation does not exist` | schema 沒被建立。`DISABLE_SCHEMA_UPDATE` 必須維持 `false`（這裡只有一個 proxy、沒有 migration job）。 |
| 模型失效，log 詢問 master/salt key | salt key 被換掉了。請從備份的 `secrets.env` 還原舊值。 |
| `ExecStartPost` 裡的 `pg_isready` 永遠不成功 | 看 `podman logs litellm-postgres`：`PGDATA` 權限錯誤或初始化到一半的 volume 都會顯示在那裡。 |
| 重開機後單元消失 | linger：`loginctl show-user "$USER" --property=Linger` 必須是 `yes`。 |
| `ql_check_unit_shadow` 中止安裝 | `~/.config/systemd/user/` 裡有與產生單元同名的檔案，優先權較高；把它移走。 |
| `podman run --replace` 刪掉了你在意的容器 | 這正是碰撞防護存在的原因；請從 `~/backups/litellm/` 還原。 |

---

## 10. 解除安裝

```bash
scripts/uninstall.sh                 # 移除單元與容器；保留 volume、network、secret、映像、env 檔
scripts/uninstall.sh --purge         # 另外刪除 volume、network 與 secret，並先做 pg_dump + secrets.env
scripts/uninstall.sh --purge-images  # 另外移除兩個映像（前提是沒有別的東西在用）
rm -rf ~/.config/litellm             # 腳本永遠不會刪除 env 檔
```

一般解除安裝後再執行 `scripts/install.sh`，會沿用同一個 volume 與 secret，
因此 gateway 會帶著原有的金鑰、團隊與花費紀錄回來。
