# 部署指南

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Podman](https://img.shields.io/badge/Podman-4.4%2B%20%7C%205.0%2B%20recommended-892CA0?logo=podman&logoColor=white)](https://podman.io/)
[![LiteLLM](https://img.shields.io/badge/LiteLLM-v1.83.14--stable-00A67E)](https://github.com/BerriAI/litellm)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16--alpine-336791?logo=postgresql&logoColor=white)](https://www.postgresql.org/)

[English](DEPLOYMENT.md) | **繁體中文**

在 Podman 上部署 WOOWTECH LiteLLM 閘道的逐步操作說明。
本文是 [`README.md`](README.md) 的長篇補充版本；此處每一項選擇背後的設計理由，
記錄在 [`docs/architecture.md`](docs/architecture.md)。

本套件支援兩條部署路徑，兩條都會從頭到尾說明：

| 路徑 | 內容 | 章節 |
|---|---|---|
| **A — `podman-compose`** | 根目錄的 `docker-compose.yml`，一道指令啟停，對 Portainer 友善 | [3](#3-路徑-a--podman-compose) |
| **B — Quadlet + `systemd --user`** | 真正的 systemd 單元、以健康檢查把關啟動順序、可開機自動啟動 | [4](#4-路徑-b--quadlet--systemd---user) |

本指南的所有內容都**未**在實際的 Podman 主機上執行過。指令是依據本儲存庫中的檔案，
以及 Podman / LiteLLM / PostgreSQL 已載於文件的行為推導而來。請把你自己主機上的
第一次啟動當成真正的測試，並用[第 6 章](#6-驗證)加以確認。

**目錄**

1. [前置需求](#1-前置需求)
2. [準備機密資訊](#2-準備機密資訊)
3. [路徑 A — podman-compose](#3-路徑-a--podman-compose)
4. [路徑 B — Quadlet + `systemd --user`](#4-路徑-b--quadlet--systemd---user)
5. [首次啟動 vs 穩定狀態 `DISABLE_SCHEMA_UPDATE`](#5-首次啟動-vs-穩定狀態-disable_schema_update)
6. [驗證](#6-驗證)
7. [日常維運](#7-日常維運)
8. [對外存取](#8-對外存取)
9. [Rootless 與 Rootful](#9-rootless-與-rootful)
10. [疑難排解](#10-疑難排解)
11. [解除安裝](#11-解除安裝)

---

## 1. 前置需求

### 1.1 Podman

| 需求 | 路徑 A（compose） | 路徑 B（Quadlet） |
|---|---|---|
| 硬性最低版本 | Podman 4.6 | Podman 4.4（Quadlet 自 4.4 起併入 Podman 核心） |
| 建議版本 | Podman 5.x | **Podman 5.0+** |
| 為什麼 5.0+ 重要 | 較新的 compose 提供者支援 | `litellm-postgres.container` 中的 `Notify=healthy` 需要 5.0+ |
| 有更好 | — | Podman 5.5+ 新增原生的 `Memory=` / `CPUQuota=` Quadlet 鍵值 |

`quadlet/install.sh` 會強制檢查這些條件：Podman 低於 4.4 時直接**結束**；
低於 5.0 時**警告**（因為 `Notify=healthy`），低於 5.5 時也會提醒
（因為這些單元檔使用 `PodmanArgs=--memory=` 而非原生的 `Memory=` 鍵值）。

```bash
# Version
podman --version

# Structured version, if you script the check
podman info --format '{{.Version.Version}}'
```

> **Podman 4.x 與 `Notify=healthy`。** 在 Podman 4.x 上，Quadlet 的 `Notify=` 鍵值
> 只接受 `true` / `false`。產生器會拒絕 `healthy` 這個值，而產生失敗的單元根本不會存在——
> 此時 `systemctl --user start litellm-postgres.service` 會回報
> *"Unit litellm-postgres.service not found"*，看起來像是檔案不見了，而不是解析錯誤。
> 如果你非得在 Podman 4.x 上執行，請把
> `quadlet/litellm-postgres.container` 裡的 `Notify=healthy` 那一行註解掉；
> `litellm-wait-postgres.service` 這個 oneshot 單元本身已經提供了就緒把關。

### 1.2 cgroups v2

兩條路徑都需要 cgroup v2。資源限制（compose 中的 `mem_limit` / `cpus`，
Quadlet 中的 `PodmanArgs=--memory/--cpus`）在 cgroup v1 的 rootless 環境下
會悄悄地變弱，或根本無法使用。

```bash
podman info --format '{{.Host.CgroupsVersion}}'
# expected: v2
```

此外，rootless 主機預設**不會**把 `cpu` 與 `cpuset` 控制器委派給使用者 slice，
因此 `--cpus=` 可能會被忽略。要啟用委派：

```bash
sudo mkdir -p /etc/systemd/system/user@.service.d
sudo tee /etc/systemd/system/user@.service.d/delegate.conf >/dev/null <<'EOF'
[Service]
Delegate=memory pids cpu cpuset
EOF
sudo systemctl daemon-reload
# log out of ALL sessions for this user and log back in
```

### 1.3 Compose 提供者 - 僅路徑 A

以下任一種都可以：

```bash
# Option 1: podman-compose >= 1.3
podman-compose --version

# Option 2: Docker Compose v2 binary driven through Podman's socket
podman compose version
```

本指南全篇都寫成 `podman-compose <cmd>`。如果你採用第二種方式，
請改成 `podman compose <cmd>`——參數完全相同。

### 1.4 真正的 `systemd --user` 工作階段 - 僅路徑 B

Quadlet 單元是由 systemd 使用者管理員逐一使用者產生的。你需要一個可運作的
使用者 bus，而不只是一個 shell：

```bash
systemctl --user is-system-running     # any answer other than "Failed to connect..." is fine
loginctl show-user "$USER" --property=Linger
echo "$XDG_RUNTIME_DIR"                # must be set, normally /run/user/<uid>
```

如果 `systemctl --user` 無法連線，你多半是在單純的 `su` 或容器 shell 裡。
請用 SSH 以目標使用者身分登入，或使用 `machinectl shell <user>@`。

### 1.5 主機資源

| 資源 | 最低需求 | 說明 |
|---|---|---|
| 記憶體 | 4 GiB | 兩個容器的上限為 2 GiB（litellm）+ 1 GiB（postgres）= 3 GiB |
| 磁碟 | 約 10 GiB 可用空間 | LiteLLM 映像檔很大；另外還有 Postgres 資料與備份 |
| CPU | 2 核心 | 限制為 `cpus: 2.0`（litellm）與 `cpus: 1.0`（postgres） |
| 網路 | 對外 HTTPS | 需連到 `ghcr.io`、`docker.io` 與 `openrouter.ai` |

### 1.6 OpenRouter 帳號

`config/config.yaml` 中的每一個模型都經由 OpenRouter 轉送
（`api_base: https://openrouter.ai/api/v1`）。你需要一個有額度的帳號與一組
API 金鑰。請到 <https://openrouter.ai/keys> 建立。金鑰以 `sk-or-` 開頭。

---

## 2. 準備機密資訊

需要四項機密資訊。在動手處理任一條路徑之前，先把它們全部產生好。

> **絕對不要把機密資訊提交進版本控制。** `.gitignore` 已經排除 `.env`、`.env.*`
>（`*.example` 檔案除外）、`*.env`、`secrets/`、`*.key`、`*.pem`、
> `backups/`、`*.sql` 與 `*.tar.gz`。請維持這個狀態。

### 2.1 `LITELLM_MASTER_KEY`

這是 Proxy 的管理憑證**同時也是** Admin UI 的密碼。必須以 `sk-` 開頭。

```bash
echo "sk-$(openssl rand -hex 32)"
```

### 2.2 `LITELLM_SALT_KEY`

```bash
echo "sk-$(openssl rand -hex 32)"
```

> ### ⚠ 設定一次，永遠不要輪替
>
> `LITELLM_SALT_KEY` 是 LiteLLM 存入 PostgreSQL 的每一組供應商憑證的加密金鑰——
> 你透過 Admin UI 新增的 OpenRouter 金鑰、日後任何供應商金鑰，
> 以及 `LiteLLM_*` 憑證資料表裡的所有內容。
>
> **只要你更動這個值，資料庫中所有已加密的憑證就會永久無法解密。**
> 沒有救援方式、無法重新推導、也沒有任何支援管道。你只能清空資料庫，
> 再逐一手動重新輸入每一組憑證。
>
> 請產生一次就好。在第一次 `up` **之前**，就把它備份到某個持久且離線的地方
>（密碼管理工具、密封信封、你的機密保管庫）。日後若要重建主機，請逐位元組還原同樣的值。
>
> 另外請注意：如果 `LITELLM_SALT_KEY` 未設定，LiteLLM 會靜默地退回使用
> `LITELLM_MASTER_KEY` 當作 salt。這表示屆時輪替主金鑰也會摧毀你已儲存的憑證。
> 請務必把兩者都明確、分別地設定。

`scripts/backup.sh` 會在每個封存檔的 `MANIFEST.txt` 中記錄 salt key 的
**指紋**（`sha256:` 加上對 `woow-litellm-salt-v1:<key>` 取 SHA-256 後的前 16 個十六進位字元）。
`scripts/restore.sh` 會拿這個指紋與執行中的堆疊比對，並在確認不一致時拒絕還原。
該指紋是單向的；金鑰本身絕不會被寫進備份。

### 2.3 `POSTGRES_PASSWORD`

```bash
openssl rand -hex 24
```

密碼中請避免使用 `:` `/` `?` `#` `[` `]` `@` 與 `%`——這些字元在 URI 中是保留字元，
放進 `DATABASE_URL` 時必須做百分比編碼。上面的十六進位輸出可以完全避開這個問題。
如果你使用 Quadlet 路徑，也請避免 `$` 與 `#`，因為 `EnvironmentFile=` 的值是由 systemd 解析的。

### 2.4 `DATABASE_URL`

這一項是推導出來的，不是產生出來的。它必須包含你剛剛產生的*同一組*密碼，
而且必須以容器名稱指向 Postgres：

```
postgresql://litellm:<POSTGRES_PASSWORD>@litellm-postgres:5432/litellm
```

`litellm-postgres` 在兩條路徑中都是容器名稱兼網路別名，所以這裡用
`localhost` 或 `127.0.0.1` **不會**成功。預設的 `podman` 網路完全沒有 DNS——
這正是兩條路徑都建立自訂橋接網路（compose 用 `litellm-network`、Quadlet 用 `litellm-net`）
的原因，這樣才能啟用 `aardvark-dns` 名稱解析。

### 2.5 `OPENROUTER_API_KEY`

請從 <https://openrouter.ai/keys> 複製。`config/config.yaml` 以
`api_key: os.environ/OPENROUTER_API_KEY` 的形式引用它，因此該值只會存在於
你的環境變數檔中——絕不會出現在設定檔裡，也絕不會進入 git。

### 2.6 總覽

| 變數 | 產生方式 | 範例檔中的佔位字串 |
|---|---|---|
| `OPENROUTER_API_KEY` | openrouter.ai/keys | `sk-or-REPLACE_ME` |
| `LITELLM_MASTER_KEY` | `echo "sk-$(openssl rand -hex 32)"` | `sk-REPLACE_ME` |
| `LITELLM_SALT_KEY` | `echo "sk-$(openssl rand -hex 32)"` | `sk-REPLACE_ME` |
| `POSTGRES_PASSWORD` | `openssl rand -hex 24` | `CHANGE_ME_TO_SECURE_PASSWORD` |
| `DATABASE_URL` | 依上述內容手動組出來 | `postgresql://litellm:CHANGE_ME_TO_SECURE_PASSWORD@litellm-postgres:5432/litellm` |

`install.sh` 與 `smoke-test.sh` 都會拒絕仍然符合
`REPLACE_ME`、`CHANGE_ME`、`PASTE_..._HERE` 或 `<your...>` 的值，
因此只填了一半的環境變數檔會立刻失敗，而不是產生令人困惑的執行期錯誤。

---

## 3. 路徑 A — podman-compose

筆記型電腦、開發機、Portainer 堆疊，或任何不需要在主機重開機後
沒有登入工作階段也能自動回復的場合，都適合這條路徑。它的形態與
其他 WOOWTECH compose 儲存庫一致。

### 3.1 複製儲存庫

```bash
git clone https://github.com/WOOWTECH/Woow_podman_litellm.git
cd Woow_podman_litellm
```

### 3.2 建立環境變數檔

```bash
cp .env.example .env
chmod 600 .env
```

`.env.example` 有 340 行以上的雙語（English / 中文）註解，圍繞著十來個變數說明。
請讀一讀——它在每個變數旁就地解釋了用途。

### 3.3 填入實際值

編輯 `.env`，用[第 2 章](#2-準備機密資訊)取得的值取代每一個佔位字串：

```bash
${EDITOR:-vi} .env
```

**必須**修改的變數（compose 檔對每一項都使用 `${VAR:?message}`，
因此只要缺少任一項，`podman-compose` 就會拒絕啟動）：

```dotenv
OPENROUTER_API_KEY=sk-or-...           # was sk-or-REPLACE_ME
LITELLM_MASTER_KEY=sk-...              # was sk-REPLACE_ME
LITELLM_SALT_KEY=sk-...                # was sk-REPLACE_ME  ← set once, never rotate
POSTGRES_PASSWORD=...                  # was CHANGE_ME_TO_SECURE_PASSWORD
DATABASE_URL=postgresql://litellm:...@litellm-postgres:5432/litellm
```

通常可以維持原樣的變數：

```dotenv
POSTGRES_USER=litellm
POSTGRES_DB=litellm
LITELLM_PORT=4000
STORE_MODEL_IN_DB=True
LITELLM_MODE=PRODUCTION
LITELLM_LOG=INFO
DISABLE_SCHEMA_UPDATE=false            # see section 5 — leave this at false
```

> 如果你更動 `POSTGRES_USER` 或 `POSTGRES_DB`，就必須連 `DATABASE_URL` 一起改。
> compose 的健康檢查是從環境變數讀取這兩個值的
>（`pg_isready -U ${POSTGRES_USER:-litellm} -d ${POSTGRES_DB:-litellm}`），
> 所以會自動跟著改變——但 Quadlet 單元是寫死的，請見
> [4.4 節](#44-安裝檔案)。

### 3.4 啟動前先驗證

```bash
export COMPOSE_PROJECT_NAME=litellm     # gives k3s-like resource names
podman-compose config >/dev/null        # 請保留重導向——原因見下方警告
```

`config` 會把所有變數代入後的合併檔案輸出出來。如果缺少必要變數，
你會看到寫在 `${VAR:?...}` 裡的訊息——例如
*"POSTGRES_PASSWORD is required - copy .env.example to .env and set a
strong value (openssl rand -hex 24)"*。請先修好再繼續。

> **切勿讓 `podman-compose config` 的 stdout 直接輸出到終端機。**
> 「所有變數代入」也包含機密變數。本堆疊是透過 `environment:` 搭配
> `${VAR:?...}` 內插來傳遞憑證，因此算繪結果會以**明文**包含
> `OPENROUTER_API_KEY`、`LITELLM_MASTER_KEY`、`LITELLM_SALT_KEY`、
> `POSTGRES_PASSWORD` 以及內含密碼的 `DATABASE_URL`。若未重導向，這些值會留在
> 終端機捲動紀錄、`tee`／CI 記錄中，也可能被貼進 GitHub issue 或客服對話裡——
> 屆時每一把金鑰都必須輪替，而 `LITELLM_SALT_KEY` 一旦輪替，資料庫中所有加密欄位
> 都將無法解密（見 §2.2）。
>
> 只要 `>/dev/null` 就夠了：你真正需要的 `${VAR:?...}` 守衛訊息是寫到 **stderr**，
> 仍然會顯示。若確實需要檢視算繪後的檔案，請寫到私有位置並立刻刪除：
>
> ```bash
> ( umask 077; podman-compose config > /tmp/compose.rendered.yml )
> less /tmp/compose.rendered.yml
> shred -u /tmp/compose.rendered.yml 2>/dev/null || rm -f /tmp/compose.rendered.yml
> ```

請把 `export COMPOSE_PROJECT_NAME=litellm` 加進你的 shell 設定檔，
或在每一道 compose 指令前面都加上它，這樣後續指令才會指向同一個專案。

### 3.5 啟動堆疊

```bash
podman-compose up -d
```

這會依序建立：

1. 橋接網路 `litellm-network`（compose 會加上專案名稱前綴，實際建立為
   `litellm_litellm-network`）；
2. 具名資料卷 `pgdata`（實際建立為 `litellm_pgdata`）；
3. 由 `postgres:16-alpine` 建立的容器 `litellm-postgres`；
4. 由 `ghcr.io/berriai/litellm:v1.83.14-stable` 建立的容器 `litellm`，
   但只有在 Postgres 回報健康之後才會啟動——compose 檔宣告了
   `depends_on: postgres: condition: service_healthy`。

### 3.6 觀察首次啟動

```bash
podman-compose ps
podman logs -f litellm
```

**第一次啟動時「正常」長什麼樣子：**

| 階段 | 耗時 | 你會看到什麼 |
|---|---|---|
| 拉取映像檔 | 1–10 分鐘 | 拉取進度；LiteLLM 映像檔很大 |
| Postgres `initdb` | 5–20 秒 | *"database system is ready to accept connections"* |
| Postgres 健康檢查 | 最多約 60 秒 | `start_period 10s`，之後每 10 秒執行 `pg_isready`，重試 5 次 |
| LiteLLM 啟動 | — | 以 Postgres 健康為前提 |
| Prisma 遷移 | 20–90 秒 | `prisma migrate deploy` 建立 `LiteLLM_*` 資料表 |
| LiteLLM 健康檢查 | `start_period` 120 秒 | 之後每 20 秒一次、重試 6 次，檢查 `/health/liveliness` |

在 `start_period` 結束之前，`podman ps` 會把 litellm 容器顯示為
`(health: starting)`。這是預期行為，**不是**失敗。請至少等滿 120 秒
再加上幾個檢查間隔，再下任何結論。

**第二次以後的啟動**會快很多：映像檔已快取、Postgres 因為
`PGDATA=/var/lib/postgresql/data/pgdata` 已經有叢集而略過 `initdb`，
`prisma migrate deploy` 也找不到待處理的遷移而幾乎等於無動作。
預期整個堆疊會在一分鐘內就健康。

### 3.7 確認可以運作

```bash
curl -s http://127.0.0.1:4000/health/liveliness
curl -s http://127.0.0.1:4000/health/readiness
```

接著執行完整檢查——請見[第 6 章](#6-驗證)：

```bash
./scripts/smoke-test.sh --mode compose
```

### 3.8 連接埠繫結

預設情況下，`docker-compose.yml` 會發布在**所有介面**上：

```yaml
ports:
  - "${LITELLM_PORT:-4000}:4000"
```

如果主機不在可信任的網路上，請改用 compose 檔中已經寫好、
但被註解掉的僅限 loopback 的版本：

```yaml
ports:
  - "127.0.0.1:${LITELLM_PORT:-4000}:4000"
```

然後在前面放一個反向代理——請見[第 8 章](#8-對外存取)。
Quadlet 路徑預設就已經只繫結 loopback。

### 3.9 生命週期指令

```bash
podman-compose ps            # status
podman-compose logs -f       # both services
podman-compose restart       # restart both
podman-compose stop          # stop, keep containers
podman-compose down          # remove containers + network — DATA IS PRESERVED
```

> `podman-compose down -v` 會連 `pgdata` 資料卷一起刪除。那會摧毀所有虛擬金鑰、
> 團隊、使用者、花費紀錄與已儲存的憑證。請見[第 11 章](#11-解除安裝)。

---

## 4. 路徑 B — Quadlet + `systemd --user`

伺服器請用這條路徑。Quadlet 會把 `quadlet/` 裡的單元檔轉成真正的 systemd 服務，
因此你會得到相依順序、以健康檢查把關的啟動流程、自動重啟、`journalctl` 整合，
以及不需登入工作階段就能開機自動啟動。這是最接近 k3s 部署的 Podman 對應做法。

以下所有內容都是 **rootless**（以你自己的非特權使用者身分執行）。
rootful 變體請見[第 9 章](#9-rootless-與-rootful)。

### 4.1 檔案各就各位

Quadlet 只會讀取 `.container`、`.volume`、`.network`、`.pod`、`.kube`、`.image`
與 `.build` 檔案。丟進 Quadlet 目錄的一般 `.service` 檔會被**靜默忽略**——
它必須改放到一般的使用者單元目錄。

| 儲存庫檔案 | 安裝到 | 原因 |
|---|---|---|
| `quadlet/litellm.network` | `~/.config/containers/systemd/` | Quadlet 單元 |
| `quadlet/litellm-pgdata.volume` | `~/.config/containers/systemd/` | Quadlet 單元 |
| `quadlet/litellm-postgres.container` | `~/.config/containers/systemd/` | Quadlet 單元 |
| `quadlet/litellm.container` | `~/.config/containers/systemd/` | Quadlet 單元 |
| `quadlet/litellm-wait-postgres.service` | `~/.config/systemd/user/` | 一般 systemd 單元，**不是** Quadlet |
| `config/config.yaml` | `~/.config/litellm/config.yaml` | 以唯讀方式 bind 掛載進 Proxy |
| `.env.quadlet.example`（填好之後） | `~/.config/litellm/litellm.env` | 兩個容器共用的 `EnvironmentFile=` |

目錄權限：`~/.config/litellm` 應為 `0700`、`litellm.env` 為 `0600`、
`config.yaml` 為 `0644`。

### 4.2 產生出來的單元名稱

**Quadlet 是從*檔名*推導 systemd 單元名稱的，不是從 `ContainerName=` /
`VolumeName=` / `NetworkName=`。** 這一點經常讓人踩雷，所以這裡列出本儲存庫的
權威對照表：

| `quadlet/` 中的檔案 | 產生的 systemd 單元 | 建立的 Podman 資源 |
|---|---|---|
| `litellm.network` | `litellm-network.service` | 網路 **`litellm-net`** |
| `litellm-pgdata.volume` | `litellm-pgdata-volume.service` | 資料卷 `litellm-pgdata` |
| `litellm-postgres.container` | `litellm-postgres.service` | 容器 `litellm-postgres` |
| `litellm.container` | `litellm.service` | 容器 `litellm` |
| `litellm-wait-postgres.service` | `litellm-wait-postgres.service`（不是產生出來的——它*本身就是*單元） | 用完即丟的容器 `litellm-wait-postgres` |

規則是：`<name>.container` → `<name>.service`；`<name>.volume` →
`<name>-volume.service`；`<name>.network` → `<name>-network.service`。

> **不要用 `NetworkName=` 來推導網路單元名稱。** `NetworkName=litellm-net`
> 只會改變 *Podman* 網路的名稱；systemd 單元仍然是依檔名而來的
> **`litellm-network.service`**。對不存在單元的 `After=` 在 systemd 中是無動作，
> 因此打錯名稱永遠不會報錯，只會悄悄失去啟動順序保證。
> 請務必在你的主機上確認真正的名稱：
>
> ```bash
> systemctl --user list-unit-files 'litellm*'
> systemctl --user list-units --all 'litellm*'
> ```

除非你另外指定，Podman 還會在資源名稱前面加上 `systemd-` 前綴。
這四個單元都明確設定了 `ContainerName=` / `VolumeName=` / `NetworkName=`，
因此資源名稱就如上表所示，而不是 `systemd-litellm` 之類。

### 4.3 準備環境變數檔

```bash
mkdir -p ~/.config/litellm
chmod 700 ~/.config/litellm
cp .env.quadlet.example ~/.config/litellm/litellm.env
chmod 600 ~/.config/litellm/litellm.env
${EDITOR:-vi} ~/.config/litellm/litellm.env
```

> **`EnvironmentFile=` 不是 shell。** `~/.config/litellm/litellm.env` 是由 systemd
> 解析的，不是被 bash source 進去的。這代表：
>
> - 只能寫單純的 `KEY=VALUE`；
> - **不支援** `${VAR}`、`${VAR:-default}` 或 `${VAR:?message}`——那些只有 compose 才有，
>   在這裡會被原封不動當成字面文字傳入；
> - **不支援**指令替換（`$(...)`、反引號）；
> - **不可以**加 `export ` 前綴（`install.sh` 會明確拒絕以它開頭的行）；
> - 值那一行的結尾不可以接 `# comment`——它會變成值的一部分；
> - 密碼中請避免 `$` 與 `#`。
>
> 另請注意，這個檔案**不**包含 `LITELLM_PORT` 或 `PGDATA`。發布的連接埠是寫死在
> `litellm.container` 裡的靜態文字（`PublishPort=127.0.0.1:4000:4000`），
> 而 `PGDATA` 是在 `litellm-postgres.container` 裡以
> `Environment=PGDATA=/var/lib/postgresql/data/pgdata` 設定的。

請設定與路徑 A 相同的五項機密資訊：
`OPENROUTER_API_KEY`、`LITELLM_MASTER_KEY`、`LITELLM_SALT_KEY`、
`POSTGRES_PASSWORD`、`DATABASE_URL`（如果你不使用 `litellm` / `litellm`，
還要加上 `POSTGRES_USER` / `POSTGRES_DB`）。

> **如果你更動了 `POSTGRES_USER` 或 `POSTGRES_DB`**，也必須修改
> `quadlet/litellm-postgres.container` 裡寫死這兩個值的健康檢查：
>
> ```ini
> HealthCmd=pg_isready -U litellm -d litellm
> ```
>
> 它們是刻意寫死的：`HealthCmd=` 中的字面 `$` 會受到 systemd 變數展開影響，
> 所以 `-U $POSTGRES_USER` 不會如你預期般運作。

### 4.4 安裝檔案

使用腳本的方式，也就是本儲存庫預期你採用的方式：

```bash
# 版控中的權限為 755，因此 git clone 下來即可執行。
# 只有在解壓 zip/tarball（權限位元會遺失）時才需要這一行。
chmod +x quadlet/*.sh scripts/*.sh

# 先驗證 unit 檔——不安裝、不啟動任何東西
./quadlet/install.sh --dry-run

# 正式執行——環境變數檔是在這一步才會被驗證
./quadlet/install.sh --env-file ~/.config/litellm/litellm.env
```

> `--dry-run` 只證明 Quadlet unit 檔可以被解析，它**不會**讀取 `--env-file`，
> 因此不會抓到殘留的佔位符或缺少的變數；那些是由正式執行檢查的，
> 一旦發現就會拒絕繼續。

`install.sh` 的旗標：

| 旗標 | 作用 |
|---|---|
| `--dry-run` | 以 Quadlet 產生器解析 `quadlet/` 下的 unit 檔以確認語法正確，然後結束；**不會**讀取 `--env-file`，也不安裝、不啟動任何東西 |
| `--env-file PATH` | 要安裝的來源環境變數檔（預設：`<repo>/.env`） |
| `--no-pull` | 略過預先拉取映像檔（首次啟動會比較慢，並可能碰到 `TimeoutStartSec`） |
| `--no-linger` | 略過 `loginctl enable-linger`（如此一來堆疊**不會**開機自動啟動） |
| `-h`、`--help` | 用法說明 |

環境變數覆寫：`LITELLM_HEALTH_TIMEOUT`（最後等待健康狀態的秒數，預設 `300`）。

它依序做的事：

1. **前置檢查**——Podman 是否存在；解析版本；低於 4.4 直接結束；低於 5.0 警告
   （`Notify=healthy`）；低於 5.5 提示（原生 `Memory=` 鍵值）；檢查 cgroups v2。
2. **驗證環境變數檔**——必要鍵值是否齊備
   （`OPENROUTER_API_KEY`、`LITELLM_MASTER_KEY`、`LITELLM_SALT_KEY`、`DATABASE_URL`、
   `POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_DB`）；拒絕殘留的佔位字串；
   拒絕 `export ` 前綴；若 `DATABASE_URL` 未指向 `litellm-postgres:5432` 或
   `postgres:5432` 則發出警告。
3. **建立目錄**——`~/.config/containers/systemd`、`~/.config/systemd/user`、
   `~/.config/litellm`。
4. **安裝 `config.yaml` 與環境變數檔**，並設定正確權限。
5. **安裝四個 Quadlet 單元與一個一般單元**，同時把
   `litellm-wait-postgres.service` 中寫死的 `/usr/bin/podman` 路徑改寫為
   `command -v podman` 回報的實際路徑。
6. **以 Quadlet 產生器 dry-run 驗證**（見 4.5）。
7. **預先拉取** `docker.io/library/postgres:16-alpine` 與
   `ghcr.io/berriai/litellm:v1.83.14-stable`。
8. 除非指定 `--no-linger`，否則執行 **`loginctl enable-linger`**。
9. **`systemctl --user daemon-reload`**，並依序啟動各單元。
10. **輪詢健康狀態**直到 `LITELLM_HEALTH_TIMEOUT` 為止。

離開代碼 `0` 表示完全成功；`1` 表示部分成功（有東西起來了，但健康狀態未能及時就緒——
請查看記錄，不要直接當成失敗）。

<details>
<summary>如果你不想執行腳本，這是等效的手動步驟</summary>

```bash
mkdir -p ~/.config/containers/systemd ~/.config/systemd/user ~/.config/litellm
chmod 700 ~/.config/litellm

install -m 0644 quadlet/litellm.network              ~/.config/containers/systemd/
install -m 0644 quadlet/litellm-pgdata.volume        ~/.config/containers/systemd/
install -m 0644 quadlet/litellm-postgres.container   ~/.config/containers/systemd/
install -m 0644 quadlet/litellm.container            ~/.config/containers/systemd/
install -m 0644 quadlet/litellm-wait-postgres.service ~/.config/systemd/user/

install -m 0644 config/config.yaml ~/.config/litellm/config.yaml
# litellm.env: create it as in 4.3, mode 0600

podman pull docker.io/library/postgres:16-alpine
podman pull ghcr.io/berriai/litellm:v1.83.14-stable
```

</details>

### 4.5 啟動任何東西之前先驗證單元檔

Quadlet 的解析錯誤只有在產生器執行時才會回報，而產生失敗的單元根本就不存在。
請先手動執行產生器：

```bash
/usr/lib/systemd/system-generators/podman-system-generator --user --dryrun
```

在某些發行版上，執行檔位於別處：

```bash
/usr/libexec/podman/quadlet --user --dryrun
/usr/lib64/systemd/system-generators/podman-system-generator --user --dryrun
```

dry run 會把每個產生的單元印到 stdout，把任何解析錯誤印到 stderr。
你應該會看到 `litellm-network.service`、`litellm-pgdata-volume.service`、
`litellm-postgres.service` 與 `litellm.service`。若少了其中一個，
就表示對應的來源檔有錯誤。

### 4.6 啟用 linger

沒有 linger 的話，你最後一個工作階段結束時 systemd 使用者管理員就會被拆除，
連帶把容器一起帶走——而且它永遠不會在開機時啟動。

```bash
loginctl enable-linger "$USER"
loginctl show-user "$USER" --property=Linger      # expect Linger=yes
```

除非你傳入 `--no-linger`，否則 `install.sh` 會替你做這件事。

### 4.7 重新載入並啟動

```bash
systemctl --user daemon-reload
```

`daemon-reload` 會重新執行 Quadlet 產生器，因此**每次**編輯
`~/.config/containers/systemd/` 中的檔案後都必須再執行一次。

依相依順序啟動：

```bash
systemctl --user start litellm-network.service
systemctl --user start litellm-pgdata-volume.service
systemctl --user start litellm-postgres.service
systemctl --user start litellm-wait-postgres.service
systemctl --user start litellm.service
```

實務上單獨執行 `systemctl --user start litellm.service` 就夠了：Quadlet 會針對
`litellm-postgres.container` 引用的 `.volume` 與 `.network` 單元自動注入
`Requires=` 與 `After=`，而 `litellm.container` 本身也宣告了
`Requires=/After=litellm-postgres.service` 以及
`Wants=/After=litellm-wait-postgres.service`。明確依序啟動只是讓故障比較容易定位。

`litellm-wait-postgres.service` 是一個 `Type=oneshot` / `RemainAfterExit=yes` 單元，
它會執行一個用完即丟的 `postgres:16-alpine` 容器，每 3 秒輪詢一次
`pg_isready -h litellm-postgres`，最長 180 秒。它是 `Notify=healthy` 這條褲子外面
再加的一條吊帶，也正是讓 Podman 4.x 的退路可行的關鍵。

### 4.8 開機自動啟動實際上是怎麼運作的

> **你無法對 Quadlet 單元執行 `systemctl --user enable`。** 產生出來的服務是暫時性的——
> 它們只存在於 `/run`，磁碟上沒有檔案可供建立符號連結，`systemctl enable` 會以
> *"unit file does not exist"* 或 *"transient or generated"* 失敗。

自動啟動其實來自另外兩件事：

1. `litellm.container` 與 `litellm-postgres.container` 內部的
   **`[Install] WantedBy=default.target multi-user.target`**。當 `daemon-reload`
   執行產生器時，systemd 會自動套用該 `[Install]` 區段，替你在 `/run` 中建立
   wants 符號連結。
2. **Linger**（4.6），它會在開機時就啟動你的 systemd 使用者管理員而不需登入，
   使其達到 `default.target`，進而把那兩個服務拉起來。

兩者都要驗證：

```bash
systemctl --user list-dependencies default.target | grep -i litellm
loginctl show-user "$USER" --property=Linger
```

若要在不解除安裝任何東西的情況下停用單一服務的自動啟動，
請刪除或註解它的 `[Install]` 區段並執行 `daemon-reload`。
若要停用整個堆疊的自動啟動，執行 `loginctl disable-linger "$USER"`。

### 4.9 檢查狀態

```bash
# systemd's view
systemctl --user status litellm-postgres.service
systemctl --user status litellm.service
systemctl --user list-units --all 'litellm*'

# Podman's view
podman ps -a --filter 'label=io.woowtech.stack=litellm-gw'
podman inspect --format '{{.State.Health.Status}}' litellm-postgres
podman inspect --format '{{.State.Health.Status}}' litellm

# Force a healthcheck to run right now instead of waiting for the next interval
podman healthcheck run litellm-postgres
podman healthcheck run litellm
```

由於 `litellm-postgres.container` 設定了 `Notify=healthy`，它產生的服務是
`Type=notify`：在容器的健康檢查首次通過之前，systemd 會把它回報為 `activating`，
通過之後才變成 `active (running)`。這正是把關下游一切的機制。
`litellm.container` 則刻意不設定 `Notify=`，改為依靠
`HealthStartupCmd`（`/health/readiness`，每 15 秒一次，最多 40 次 = 10 分鐘）
加上一般的 `HealthCmd`（`/health/liveliness`，每 20 秒一次、重試 6 次、
`HealthStartPeriod=120s`）。

### 4.10 記錄

```bash
# Follow the proxy
journalctl --user -u litellm.service -f

# Follow Postgres
journalctl --user -u litellm-postgres.service -f

# The readiness gate writes with SyslogIdentifier=litellm-wait-postgres
journalctl --user -t litellm-wait-postgres -n 100

# Everything from this stack since the last boot
journalctl --user -u 'litellm*' -b

# Podman's own container logs (equivalent content, different plumbing)
podman logs -f litellm
podman logs --tail 200 litellm-postgres
```

### 4.11 重啟策略

Quadlet **不會**為 `.container` 單元產生 `Restart=` 指示，所以兩個容器單元都自行設定：

```ini
[Service]
Restart=always
RestartSec=10
```

`StartLimitIntervalSec=300` / `StartLimitBurst=5` 會限制重啟迴圈——五分鐘內失敗五次，
systemd 就會停止嘗試，這是最接近 Kubernetes `CrashLoopBackOff` 的對應機制。
修好原因之後要清除該狀態：

```bash
systemctl --user reset-failed litellm.service
systemctl --user start litellm.service
```

`TimeoutStartSec` 被調得遠高於 systemd 預設的 90 秒（Postgres 300 秒、
LiteLLM 600 秒），因為冷啟動拉取映像檔加上首次啟動的 Prisma 遷移，
輕易就會超過 90 秒。

---

## 5. 首次啟動 vs 穩定狀態 `DISABLE_SCHEMA_UPDATE`

這是最常見、會讓堆疊乾淨啟動之後卻在每個請求都回報資料庫錯誤的原因。
請在第一次 `up` 之前讀完本章。

### 5.1 這個變數的作用

LiteLLM 的 entrypoint 在啟動時會針對 `DATABASE_URL` 執行一個 Prisma 步驟。
執行哪一個步驟取決於 `DISABLE_SCHEMA_UPDATE`：

| 值 | 執行什麼 | 效果 |
|---|---|---|
| `false`（LiteLLM 的預設值） | `prisma migrate deploy` | 套用待處理的遷移——**建立 `LiteLLM_*` 資料表** |
| `true` | `prisma migrate diff` | 唯讀。印出*將會*需要的 SQL。**不會建立任何東西。** |

陷阱在於：當 `DISABLE_SCHEMA_UPDATE=true` 且資料庫是**空的**時，容器會啟動、
通過 `/health/liveliness`，看起來完全正常——因為 liveliness 探針只證明行程還活著。
接著第一個碰到資料庫的請求就會以
`relation "LiteLLM_VerificationToken" does not exist` 之類的錯誤失敗。

另請注意 LiteLLM 對「真值」字串的解析很寬鬆：`true`、`1`、`t`、`y` 與 `yes`
都代表 true。`DISABLE_SCHEMA_UPDATE=1` 是*開啟*，不是「版本 1」。

### 5.2 本儲存庫的做法

**兩條路徑都以 `DISABLE_SCHEMA_UPDATE=false` 出貨，而你應該就讓它維持這樣。**

- `docker-compose.yml`：`DISABLE_SCHEMA_UPDATE: "${DISABLE_SCHEMA_UPDATE:-false}"`
- `.env.example`：`DISABLE_SCHEMA_UPDATE=false`
- `quadlet/litellm.container`：`Environment=DISABLE_SCHEMA_UPDATE=false`

這是**刻意偏離 k3s manifest** 的做法，k3s 那邊設定為 `true`。差異的原因是：

- 在 k3s 上，schema 管理是在頻外處理的——由獨立的遷移 Job 對資料庫執行，
  而長時間存活的 Proxy Pod 則刻意被剝奪變更 schema 的能力。
  在那裡，多個副本在滾動更新時競爭 `migrate deploy` 是真實存在的風險。
- 在這裡只有**一個** Proxy 容器，沒有滾動更新，也沒有獨立的遷移 Job。
  總得有人來建立 schema，而在單一容器裡執行 `migrate deploy` 就是那個角色。

**不要把 k3s manifest 的 `true` 抄過來。** 如果你已經這麼做並碰到症狀，
修法是：

```bash
# Path A
sed -i 's/^DISABLE_SCHEMA_UPDATE=.*/DISABLE_SCHEMA_UPDATE=false/' .env
podman-compose up -d --force-recreate litellm

# Path B — the value is in the unit, not the env file
${EDITOR:-vi} ~/.config/containers/systemd/litellm.container   # Environment=DISABLE_SCHEMA_UPDATE=false
systemctl --user daemon-reload
systemctl --user restart litellm.service
```

### 5.3 什麼時候 `true` 才是正確答案

只有在 schema 已經存在，**而且**你想把它釘住的時候——例如你升級了 LiteLLM 映像檔，
想在遷移執行前先審視 SQL，或是 schema 由你的 DBA 掌管。在那種工作流程中：

1. 設定 `DISABLE_SCHEMA_UPDATE=true` 並重啟。
2. 讀取容器啟動時印出的 SQL（`prisma migrate diff` 的輸出）。
3. 自行套用它，或把旗標切回 `false` 重啟一次，然後再切回去。

本套件的穩定狀態是 `false`。它是冪等的：在第一次之後的每次啟動，
`migrate deploy` 都會找不到待處理項目而什麼也不做。

### 5.4 確認資料庫結構已存在

`scripts/smoke-test.sh` 的第 7 項檢查存在的目的正是為了抓到這個問題。手動做法：

```bash
podman exec litellm-postgres psql -U litellm -d litellm -tAc \
  "SELECT count(*) FROM information_schema.tables
    WHERE table_schema='public' AND table_name LIKE 'LiteLLM%';"
```

健康的堆疊會回傳一個遠大於零的數字。`0` 表示遷移從未執行過——
請檢查 `DISABLE_SCHEMA_UPDATE`，然後用 `podman logs litellm` 查看 Prisma 的輸出。

---

## 6. 驗證

### 6.1 冒煙測試

```bash
./scripts/smoke-test.sh                    # auto-detects the deployment mode
./scripts/smoke-test.sh --mode compose
./scripts/smoke-test.sh --mode quadlet
./scripts/smoke-test.sh --env-file ~/.config/litellm/litellm.env
./scripts/smoke-test.sh --help
```

| 離開代碼 | 意義 |
|---|---|
| `0` | 全部 7 項檢查通過 |
| `1` | 有一項以上檢查失敗 |
| `2` | 用法錯誤／無法判斷環境 |

環境變數覆寫：`PODMAN`（執行檔路徑）、`LITELLM_CONTAINER`
（預設 `litellm`）、`POSTGRES_CONTAINER`（預設 `litellm-postgres`）。

模式自動偵測的順序為：容器內有 `PODMAN_SYSTEMD_UNIT=` 變數 ⇒ quadlet；
有 `com.docker.compose.project=` / `io.podman` 標籤 ⇒ compose；
`systemctl --user is-active litellm.service` ⇒ quadlet；
儲存庫本地有 `docker-compose.yml` ⇒ compose。

環境變數檔的探查會跟著模式走：quadlet 先找
`~/.config/litellm/litellm.env` 再找 `<repo>/.env`；compose 則以相反順序尋找。
該檔案是被**解析**的，絕不會被 source，而主金鑰是透過一個權限 `0600`、
並由 `EXIT` trap 移除的暫存檔交給 `curl`——它絕不會出現在 `ps` 輸出或你的 shell 歷史中。

### 6.2 七項檢查與對應的手動指令

**檢查 1 — 兩個容器都在執行**

```bash
podman ps --format '{{.Names}}'
# expect litellm and litellm-postgres
```

**檢查 2 — Postgres 健康檢查為 healthy**

```bash
podman inspect --format '{{.State.Health.Status}}' litellm-postgres
# expect: healthy

# If it is not, trigger a check immediately rather than waiting 10s
podman healthcheck run litellm-postgres

# The underlying probe, run by hand
podman exec litellm-postgres pg_isready -U litellm -d litellm
```

**檢查 3 — LiteLLM 健康檢查為 healthy**

```bash
podman inspect --format '{{.State.Health.Status}}' litellm
podman healthcheck run litellm
```

冒煙測試會把 `starting` 單獨回報，並指出可能是首次啟動的 Prisma 遷移。
在冷啟動的第一次，請等滿 `HealthStartPeriod=120s` 再把它當成故障。

**檢查 4 — liveliness**

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4000/health/liveliness
# expect 200
```

只檢查行程。它證明 uvicorn 有在回應，但完全無法證明資料庫的狀況。

**檢查 5 — readiness**

```bash
curl -sS http://127.0.0.1:4000/health/readiness | python3 -m json.tool
```

預期得到 HTTP 200 與 JSON 中的 `db` 欄位。readiness 是有感知資料庫的，
在 Postgres 無法連線時會回傳 **503**——這才是真正告訴你堆疊可用的探針。

**檢查 6 — 模型清單**

```bash
read -rsp 'master key: ' LITELLM_MASTER_KEY; echo
curl -sS http://127.0.0.1:4000/v1/models \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" | python3 -m json.tool
```

預期得到 200 以及一個非空的 `data` 陣列，內含 `config/config.yaml` 中
每一個未被註解的 `model_name`：

| `model_name` | 轉送到 |
|---|---|
| `gpt-4o-mini` | `openrouter/openai/gpt-4o-mini` |
| `glm-4.6` | `openrouter/z-ai/glm-4.6` |
| `minimax-m2` | `openrouter/minimax/minimax-m2` |
| `claude-sonnet-4.5` | `openrouter/anthropic/claude-sonnet-4.5` |
| `llama-3.3-70b` | `openrouter/meta-llama/llama-3.3-70b-instruct` |

之後透過 Admin UI 新增的額外項目沒有關係——冒煙測試只要求檔案裡的模型全都存在。

**檢查 7 — 資料庫 schema 已存在**

```bash
podman exec litellm-postgres psql -U litellm -d litellm -tAc \
  "SELECT count(*) FROM information_schema.tables
    WHERE table_schema='public' AND table_name LIKE 'LiteLLM%';"
```

請見 [5.4 節](#54-確認資料庫結構已存在)。

**參考用（不列入計數）— 發布的連接埠有回應**

```bash
timeout 3 bash -c 'exec 3<>/dev/tcp/127.0.0.1/4000 && echo open'
```

### 6.3 端對端的一次真實請求

上述檢查都不會消耗 OpenRouter 額度。要證明整條路徑都能運作，
請送出一個真正的請求——這**會**花掉不到一分錢：

```bash
curl -sS http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"ping"}]}' \
  | python3 -m json.tool
```

### 6.4 兩件不要做的事

> **不要探測單純的 `/health`。** 與 `/health/liveliness` 和 `/health/readiness`
> 不同，`/health` 需要主金鑰**而且**會對每一個已設定的模型發出真正的請求。
> 它很慢，而且每次執行都會燒掉 OpenRouter 額度。
> 絕對不要把它接進健康檢查或監控迴圈。

> **不要在 LiteLLM 容器內使用 `curl`。** 該映像檔內含 Python，
> 但**沒有** curl。這正是兩個健康檢查都使用 Python 單行指令的原因：
>
> ```bash
> podman exec litellm python -c \
>   "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:4000/health/liveliness').status==200 else 1)"
> ```
>
> 從主機上執行單純的 `curl http://127.0.0.1:4000/...` 則沒有問題。

---

## 7. 日常維運

### 7.1 記錄

| | 路徑 A（compose） | 路徑 B（Quadlet） |
|---|---|---|
| 追蹤 Proxy | `podman-compose logs -f litellm` | `journalctl --user -u litellm.service -f` |
| 追蹤 Postgres | `podman-compose logs -f postgres` | `journalctl --user -u litellm-postgres.service -f` |
| 最後 200 行 | `podman logs --tail 200 litellm` | `podman logs --tail 200 litellm` |
| 本次開機以來 | — | `journalctl --user -u 'litellm*' -b` |

`podman logs <container>` 在兩條路徑中的行為完全相同。

要提高詳細程度，請設定 `LITELLM_LOG=DEBUG`——路徑 A 設在 `.env`，路徑 B 則要改
`quadlet/litellm.container` 中的 `Environment=LITELLM_LOG=` 那一行（在該路徑下寫進
env 檔沒有作用，因為 `--env` 會覆蓋 `--env-file`）。請留意 LiteLLM 命名上的偏移：
`LITELLM_LOG=INFO`（出貨預設值）大致上已經等同於 CLI 所說的 `--debug`，
而 `DEBUG` 比你預期的還要吵得多。用完請調回去——除錯記錄可能包含請求內容。

### 7.2 重啟

```bash
# Path A
podman-compose restart litellm
podman-compose restart                 # both services

# Path B
systemctl --user restart litellm.service
systemctl --user restart litellm-postgres.service   # restarts litellm too, via Requires=
```

重啟 Postgres 會把 Proxy 一起拉下來，因為 `litellm.container` 宣告了
`Requires=litellm-postgres.service`。只重啟 Proxy 是安全的，
而且那正是設定變更時你要的做法。

### 7.3 更新映像檔

兩條路徑都釘住精確的標籤（`ghcr.io/berriai/litellm:v1.83.14-stable`、
`postgres:16-alpine`）。沒有任何東西會自動更新：Quadlet 單元中的
`AutoUpdate=registry` 與 `Pull=` 都刻意保持未設定。升級是由你決定的事。

**請務必先備份**——請見 [7.5](#75-備份)。LiteLLM 升級可能會對你的資料庫執行新的
Prisma 遷移，而那些遷移並不容易還原。

**路徑 A：**

```bash
# 1. edit the tag
${EDITOR:-vi} docker-compose.yml        # services.litellm.image

# 2. fetch it
podman-compose pull

# 3. recreate with the new image
podman-compose up -d

# 4. verify
./scripts/smoke-test.sh --mode compose
```

**路徑 B：**

```bash
# 1. edit the tag in the repo copy AND the installed copy
${EDITOR:-vi} quadlet/litellm.container
${EDITOR:-vi} ~/.config/containers/systemd/litellm.container    # Image=

# 2. fetch it before restarting, so TimeoutStartSec is not spent pulling
podman pull ghcr.io/berriai/litellm:NEW_TAG_YOU_JUST_SET

# 3. regenerate the units and restart
systemctl --user daemon-reload
systemctl --user restart litellm.service

# 4. verify
./scripts/smoke-test.sh --mode quadlet
```

重新執行 `./quadlet/install.sh` 同樣可行，而且比較不容易出錯，
因為它會把儲存庫中的單元覆蓋到已安裝的單元上、預先拉取、重新載入並重啟。

**回滾**是同樣的程序，只是換成舊標籤。如果新版本執行過遷移，
也請一併從備份還原資料庫。

為了可重現性，你可以改用 digest 而非標籤來釘住版本——
`quadlet/litellm-postgres.container` 內附有一個被註解掉的範例
（`Image=...@sha256:PASTE_DIGEST_HERE`）。用以下指令找出 digest：

```bash
podman image inspect docker.io/library/postgres:16-alpine \
  --format '{{index .RepoDigests 0}}'
```

### 7.4 舊映像檔

```bash
podman images
podman image prune          # dangling only
podman system df            # what is actually using disk
```

### 7.5 備份

```bash
./scripts/backup.sh                                 # auto-detect mode, output to <repo>/backups
./scripts/backup.sh --mode quadlet --out /srv/backups
./scripts/backup.sh --env-file ~/.config/litellm/litellm.env
./scripts/backup.sh --help
```

會產生 `<out>/litellm-backup-YYYYmmdd-HHMMSS.tar.gz`，權限 `0600`，
放在以 `0700` 建立的目錄中。內容為：

| 成員 | 是什麼 |
|---|---|
| `database.dump` | `pg_dump --format=custom --no-owner --no-privileges` |
| `config.yaml` | 現行 `config.yaml` 的副本 |
| `MANIFEST.txt` | 中繼資料（見下方） |

該 manifest 會記錄 `backup_format=1`、`created_utc`、`deploy_mode`、
`podman_version`、`litellm_image`、`postgres_image`、`litellm_tables`、
`dump_bytes` 與 `salt_key_fingerprint`。腳本會驗證傾印檔的 `PGDMP` 魔術位元組，
並在計算到零個 `LiteLLM_*` 資料表時大聲警告（那代表你剛剛備份了一個空資料庫）。

> **這個封存檔刻意不包含 `.env` 或 `litellm.env`。** 你的機密資訊不在備份裡。
> 這是刻意的——備份封存檔會被到處複製，它不應該變成憑證外洩的來源。
> 這也意味著**光靠備份無法還原你的堆疊**：你必須另外保存 `LITELLM_SALT_KEY`，
> 否則 `database.dump` 中所有已加密的內容都無法讀取。請見
> [2.2 節](#22-litellm_salt_key)。

等效的手動做法：

```bash
podman exec litellm-postgres pg_dump -U litellm -d litellm \
  --format=custom --no-owner --no-privileges > database.dump
```

請用使用者 timer 或 cron 排程它，並把封存檔複製到主機之外。

### 7.6 還原

```bash
./scripts/restore.sh backups/litellm-backup-20260806-101500.tar.gz
./scripts/restore.sh <archive> --mode quadlet
./scripts/restore.sh <archive> --restore-config
./scripts/restore.sh <archive> --force-salt-mismatch
./scripts/restore.sh <archive> --yes
```

| 離開代碼 | 意義 |
|---|---|
| `0` | 還原完成 |
| `1` | 還原失敗，或被拒絕（例如 salt key 不一致） |
| `2` | 用法錯誤 |
| `3` | 操作者在確認提示處中止 |

它做的事：

1. 檢視封存檔並讀取 `MANIFEST.txt`。
2. **Salt key 把關**——把 manifest 中的 `salt_key_fingerprint` 與執行中堆疊的比對。
   在*確認*不一致時會**拒絕**執行，除非你傳入 `--force-salt-mismatch`。
   若其中任一指紋未知，它會大聲警告後繼續。強行覆寫的代價是：
   還原後的憑證資料列會變成無法解密的亂碼；你已經被警告兩次了。
3. 要求你輸入 `RESTORE`（加上 `--yes` 可略過）。
4. **只**停止 Proxy。Postgres 保持運行——它是還原的目標。
5. 對維護資料庫 `postgres` 執行：
   `DROP DATABASE IF EXISTS "litellm" WITH (FORCE);` 接著
   `CREATE DATABASE "litellm" OWNER "litellm";`
   （`WITH (FORCE)` 需要 PostgreSQL 13+，`16-alpine` 已滿足。）
6. `pg_restore -U litellm -d litellm --no-owner --no-privileges --single-transaction`
   ——必要時可用 `PG_RESTORE_ARGS` 環境變數覆寫這些旗標。
7. 重啟 Proxy 並計算還原出來的 `LiteLLM_*` 資料表數量。

`--restore-config` 還會用封存檔中的內容覆寫 `<repo>/config/config.yaml`。
在 Quadlet 路徑上這樣做並不足夠，因為 Proxy 讀的是
`~/.config/litellm/config.yaml`；腳本會就此提出警告。請把它複製過去：

```bash
install -m 0644 config/config.yaml ~/.config/litellm/config.yaml
systemctl --user restart litellm.service
```

最後請執行 `./scripts/smoke-test.sh` 收尾，腳本本身也會這樣建議。

### 7.7 調整資源限制

**路徑 A**——編輯 `docker-compose.yml` 並重新建立：

```yaml
services:
  litellm:
    mem_limit: 4g
    cpus: 4.0
```

```bash
podman-compose up -d
```

這裡使用 `mem_limit` / `cpus` 而非 `deploy.resources.limits`，
是因為 podman-compose 在 Swarm 之外並不可靠地遵循 `deploy:` 區塊。

**路徑 B**——編輯單元檔並重新載入：

```ini
[Container]
PodmanArgs=--memory=4g
PodmanArgs=--cpus=4.0
```

```bash
${EDITOR:-vi} ~/.config/containers/systemd/litellm.container
systemctl --user daemon-reload
systemctl --user restart litellm.service
```

在 Podman 5.5+ 上你可以改用原生的 `Memory=4g` 鍵值，而不是
`PodmanArgs=--memory=4g`。這些單元使用 `PodmanArgs=` 是為了在 5.0–5.4 上也能運作。

> rootless 的 `--cpus=` 需要 `cpu` 控制器已委派給你的使用者 slice。
> 若沒有，Podman 可能會警告或靜默忽略該限制。請見
> [1.2 節](#12-cgroups-v2)。確認實際生效的設定：
>
> ```bash
> podman inspect --format '{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}' litellm
> podman stats --no-stream
> ```

### 7.8 新增或變更模型

模型定義在 `config/config.yaml`。在 `model_list` 中新增一個項目：

```yaml
model_list:
  - model_name: my-new-model
    litellm_params:
      model: openrouter/vendor/model-slug
      api_key: os.environ/OPENROUTER_API_KEY
      api_base: https://openrouter.ai/api/v1
```

> 重啟之前請先驗證 slug。OpenRouter 會淘汰 slug——這份設定檔曾經使用的
> `anthropic/claude-3.5-sonnet` 這個裸 slug 後來開始回傳
> **404 "No endpoints found"**，這正是它被改指向並更名為
> `claude-sonnet-4.5` 的原因。請對照即時目錄檢查：
>
> ```bash
> curl -s https://openrouter.ai/api/v1/models | python3 -c \
>   "import json,sys; [print(m['id']) for m in json.load(sys.stdin)['data']]" | grep -i <vendor>
> ```

**套用變更——路徑 A：**

```bash
${EDITOR:-vi} config/config.yaml
podman-compose restart litellm
```

該檔案是 bind 掛載的（`./config/config.yaml:/app/config.yaml:ro,Z`），
所以不需要重建、也不需要 `up -d`——只要重啟讓它重新讀取即可。

**套用變更——路徑 B：**

```bash
${EDITOR:-vi} config/config.yaml
install -m 0644 config/config.yaml ~/.config/litellm/config.yaml
systemctl --user restart litellm.service
```

Quadlet 單元掛載的是 `%h/.config/litellm/config.yaml`，也就是*已安裝*的副本——
只編輯儲存庫中的副本不會有任何效果。兩條路徑都把該檔案掛載到容器內的同一個路徑
`/app/config.yaml`，也都傳入 `--config /app/config.yaml --port 4000` 作為參數。

**兩種情況下：Postgres 都不需要重啟。** 只有 Proxy 會重新讀取 `config.yaml`。

> **config.yaml 與資料庫如何互動。** 兩條路徑都設定了
> `store_model_in_db: true` / `STORE_MODEL_IN_DB=True`，因此透過 Admin UI 新增的模型
> 會被保存在 PostgreSQL。對多數設定而言，資料庫優先。
> **`model_list` 是例外：檔案中的模型與資料庫中的模型是合併，而不是取代。**
> 因此在 `config.yaml` 中定義的模型無法從 Admin UI 刪除——它每次重啟都會再出現。
> 要移除它，請刪除或註解 `config.yaml` 中的該項目，然後重啟 Proxy。

> **不要加上 `entrypoint:`，也不要在命令前面加 `litellm`。** 該映像檔的
> `ENTRYPOINT` 是 `docker/prod_entrypoint.sh`，它結尾是 `exec litellm "$@"`。
> compose 的 `command:` 與 Quadlet 的 `Exec=` 只提供*參數*。

---

## 8. 對外存取

> ## ⚠ 不要重複使用現有 k3s 部署的 CLOUDFLARE TUNNEL TOKEN
>
> **本套件刻意不提供 `cloudflared` 容器、不提供 tunnel 服務、也不提供
> `TUNNEL_TOKEN` 變數。** 這不是疏漏。
>
> Cloudflare tunnel token 識別的是一條 tunnel，而不是一個連接器。
> 用**同一組** token 啟動第二個 `cloudflared`，等於在**同一條** tunnel 上
> 註冊了第二個連接器，Cloudflare 接著就會把正式流量在兩者之間做負載平衡——
> 把無法預測比例的線上請求導進這套 Podman 堆疊，而它有著不同的資料庫、
> 不同的虛擬金鑰與不同的花費紀錄。
>
> 現有 k3s 部署所使用的那組 token *此時此刻*正在使用中。把它貼到這裡會分裂
> 線上流量，並可能弄壞該部署。**絕對不要這麼做。**
>
> 如果你確實需要為這套堆疊建立 Cloudflare tunnel，請在 Cloudflare 控制台中
> 建立一條**全新、獨立**的 tunnel，配上它自己的 token 與自己的主機名稱，
> 並在本儲存庫之外自行執行 `cloudflared`。這套堆疊在任何情況下都不是
> 現有 k3s 部署主機名稱的服務目標。

預設情況下，Proxy 只能從主機本機存取：

| 路徑 | 繫結方式 |
|---|---|
| A（compose） | `${LITELLM_PORT:-4000}:4000`——預設為**所有介面**；檔案中另附有一行被註解掉、僅限 loopback 的寫法 |
| B（Quadlet） | `PublishPort=127.0.0.1:4000:4000`——僅限 loopback |

以下記載三種對外開放的方式。請擇一使用，不要疊加。

### 8.1 反向代理 建議做法

在同一台主機上以代理伺服器終結 TLS，再轉送到 `127.0.0.1:4000`。
容器維持繫結在 loopback。

**nginx：**

```nginx
server {
    listen 443 ssl http2;
    server_name llm.example.internal;

    ssl_certificate     /etc/letsencrypt/live/llm.example.internal/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/llm.example.internal/privkey.pem;

    # LLM responses are slow; the config sets request_timeout: 600
    proxy_connect_timeout 60s;
    proxy_send_timeout    620s;
    proxy_read_timeout    620s;

    location / {
        proxy_pass http://127.0.0.1:4000;

        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection        "";

        # Required for streaming (stream: true) responses to arrive incrementally
        proxy_buffering off;
        proxy_cache off;

        client_max_body_size 32m;
    }
}
```

**Caddy**（會自動取得並更新 TLS 憑證）：

```caddyfile
llm.example.internal {
    reverse_proxy 127.0.0.1:4000 {
        # -1 disables response buffering — needed for streaming
        flush_interval -1
        transport http {
            read_timeout  620s
            write_timeout 620s
        }
    }
}
```

如果 SELinux 處於 enforcing 模式，請允許代理伺服器建立對外網路連線：

```bash
sudo setsebool -P httpd_can_network_connect 1
```

`proxy_buffering off` / `flush_interval -1` 不是可選項。少了它們，
server-sent-event 串流回應會被緩衝，等到最後才一次全部送達，
在用戶端看起來就像卡住了。

### 8.2 Tailscale（或其他 WireGuard mesh）

對於完全不需要公開曝光的小團隊，這是最好的選項。在主機上安裝 Tailscale，
容器維持在 loopback，然後透過 tailnet 存取：

```bash
# On the host
sudo tailscale up

# Bind the proxy to the tailnet address instead of loopback:
#   compose:  ports: - "100.x.y.z:4000:4000"
#   quadlet:  PublishPort=100.x.y.z:4000:4000
```

或者讓它留在 loopback，改用 Tailscale Serve，這樣監聽端仍然只對 tailnet
私有，並且會加上 TLS：

```bash
tailscale serve --bg --https=443 http://127.0.0.1:4000
```

請用 tailnet ACL 限制誰能存取。**不要**在這裡使用 `tailscale funnel`——
那會發布到公開網際網路上。

### 8.3 可信任 LAN 的連接埠曝光（最後手段）

只能在你完全掌控的網路上使用，而且前面一定要有主機防火牆：

```bash
# Compose already binds 0.0.0.0 by default; for Quadlet, edit the unit:
#   PublishPort=0.0.0.0:4000:4000
# then: systemctl --user daemon-reload && systemctl --user restart litellm.service

# firewalld: allow one subnet only
sudo firewall-cmd --permanent --new-zone=litellm
sudo firewall-cmd --permanent --zone=litellm --add-source=192.168.10.0/24
sudo firewall-cmd --permanent --zone=litellm --add-port=4000/tcp
sudo firewall-cmd --reload
```

這是明文 HTTP，而主金鑰會放在 `Authorization` 標頭中傳輸。
絕對不要在不可信任的網路上這麼做，也絕對不要用在公開 IP 上。

### 8.4 無論你選哪一種

- 請為呼叫端核發受限範圍的**虛擬金鑰**，而不是直接把
  `LITELLM_MASTER_KEY` 發出去：

  ```bash
  curl -sS http://127.0.0.1:4000/key/generate \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    -H 'Content-Type: application/json' \
    -d '{"models":["gpt-4o-mini"],"max_budget":5,"key_alias":"demo"}'
  ```

- 請記得 `LITELLM_MASTER_KEY` 同時也是 Admin UI 的密碼。任何人拿著它連上 UI，
  就等於握有這座閘道與它的花費。
- 如果端點會被超過寥寥幾個用戶端存取，請在代理層做速率限制。

---

## 9. Rootless 與 Rootful

**Rootless 是本套件建議且預設的組態**，也是 `quadlet/install.sh` 與
`quadlet/uninstall.sh` 所實作的方式。容器逃逸落到的是一個非特權使用者帳號，
而不是主機上的 `root`；資料卷、網路與單元檔都由該使用者擁有；
而且這套堆疊沒有任何東西需要特權連接埠或主機裝置。

只有在你有硬性需求時才使用 rootful——例如你必須直接繫結 443 連接埠、
需要主機裝置，或公司政策要求使用系統層級的單元。

| | Rootless（預設） | Rootful |
|---|---|---|
| Quadlet 單元目錄 | `~/.config/containers/systemd/` | `/etc/containers/systemd/` |
| 一般單元目錄 | `~/.config/systemd/user/` | `/etc/systemd/system/` |
| 控制指令 | `systemctl --user ...` | `sudo systemctl ...` |
| 記錄 | `journalctl --user -u ...` | `sudo journalctl -u ...` |
| 重新載入 | `systemctl --user daemon-reload` | `sudo systemctl daemon-reload` |
| `[Install] WantedBy=` | `default.target` | `multi-user.target` |
| 開機自動啟動需要 | `loginctl enable-linger $USER` | 不需額外設定 |
| `%h` 展開為 | 你的家目錄 | `/root` |
| 環境變數檔位置 | `~/.config/litellm/litellm.env` | `/etc/litellm/litellm.env` |
| 1024 以下的連接埠 | 預設被封鎖 | 允許 |
| Podman 指令 | `podman ...` | `sudo podman ...` |
| 資料卷在磁碟上的位置 | `~/.local/share/containers/storage/volumes/` | `/var/lib/containers/storage/volumes/` |

這些單元出貨時帶有 `WantedBy=default.target multi-user.target`——兩者都有，
所以同一個檔案在兩種模式下都能用。

### 9.1 轉換成 rootful

```bash
# 1. Env file and config in a system location
sudo install -d -m 700 /etc/litellm
sudo install -m 600 .env.quadlet.example /etc/litellm/litellm.env   # then fill it in
sudo install -m 644 config/config.yaml   /etc/litellm/config.yaml

# 2. Units into the system Quadlet directory
sudo install -m 644 quadlet/litellm.network            /etc/containers/systemd/
sudo install -m 644 quadlet/litellm-pgdata.volume      /etc/containers/systemd/
sudo install -m 644 quadlet/litellm-postgres.container /etc/containers/systemd/
sudo install -m 644 quadlet/litellm.container          /etc/containers/systemd/
sudo install -m 644 quadlet/litellm-wait-postgres.service /etc/systemd/system/

# 3. Repoint the paths: %h is /root under rootful, so change
#      EnvironmentFile=%h/.config/litellm/litellm.env  ->  /etc/litellm/litellm.env
#      Volume=%h/.config/litellm/config.yaml:...       ->  /etc/litellm/config.yaml:...
#    in both .container files.

# 4. Validate, reload, start
sudo /usr/lib/systemd/system-generators/podman-system-generator --dryrun
sudo systemctl daemon-reload
sudo systemctl start litellm.service
sudo systemctl status litellm.service
```

`quadlet/install.sh` 與 `quadlet/uninstall.sh` 只支援 rootless。
在 rootful 模式下請依上述方式手動管理檔案。

### 9.2 特權連接埠

Rootless Podman 預設無法繫結 1024 以下的連接埠。建議做法：讓 Proxy 留在 4000，
並在反向代理上終結 443（[8.1 節](#81-反向代理-建議做法)）。

如果你真的必須在 rootless 下繫結低號連接埠：

```bash
echo 'net.ipv4.ip_unprivileged_port_start=80' | \
  sudo tee /etc/sysctl.d/99-unprivileged-ports.conf
sudo sysctl --system
```

這會降低主機上**每一個**非特權行程的門檻。請優先使用反向代理。

---

## 10. 疑難排解

| 症狀 | 可能原因 | 解法 |
|---|---|---|
| `litellm-postgres` 一直停在 `unhealthy` | `pg_isready` 被以錯誤的使用者／資料庫執行。Quadlet 的 `HealthCmd=` 寫死了 `-U litellm -d litellm`。 | 手動執行 `podman exec litellm-postgres pg_isready -U litellm -d litellm`。如果你改過 `POSTGRES_USER` / `POSTGRES_DB`，請把 `~/.config/containers/systemd/litellm-postgres.container` 中的 `HealthCmd=` 改成一致，然後執行 `systemctl --user daemon-reload && systemctl --user restart litellm-postgres.service`。 |
| Postgres 啟動時以 *"directory ... exists but is not empty"* 結束 | 資料目錄中含有並非叢集的檔案。 | 這些單元設定 `PGDATA=/var/lib/postgresql/data/pgdata` 正是為了避免這件事（entrypoint 拒絕對非空目錄執行 `initdb`）。請確認 `PGDATA` 有設定；若資料卷確實已損毀而你有備份，請移除資料卷後還原。 |
| `litellm` 在首次啟動時卡在 `starting` 好幾分鐘 | 正常現象。`HealthStartPeriod=120s`，再加上龐大的映像檔拉取與首次啟動的 Prisma 遷移。 | 等待。用 `podman logs -f litellm` 觀察進度。只有在 `HealthStartupRetries=40 × 15s`（10 分鐘）都過完之後，才把它當成故障。 |
| `litellm` 始終無法變成 healthy | 連不到資料庫，或 `DATABASE_URL` 有誤。 | 執行 `curl -s http://127.0.0.1:4000/health/readiness`——回傳 503 且 `db` 欄位失敗即可確認。檢查 `DATABASE_URL` 是否指向 `litellm-postgres:5432`（**不是** `localhost`），以及密碼是否與 `POSTGRES_PASSWORD` 相符。 |
| `relation "LiteLLM_..." does not exist` | 對空資料庫使用了 `DISABLE_SCHEMA_UPDATE=true`——`prisma migrate diff` 印出了 SQL 但什麼也沒建立。 | 設成 `false` 並重啟 Proxy。請見[第 5 章](#5-首次啟動-vs-穩定狀態-disable_schema_update)。用 [6.2 檢查 7](#62-七項檢查與對應的手動指令) 的資料表計數查詢加以驗證。 |
| Proxy 回傳 `401 Unauthorized` | 缺少或錯誤的 `Authorization` 標頭，或你的 shell 與容器中的 `LITELLM_MASTER_KEY` 不同。 | 送出 `-H "Authorization: Bearer $LITELLM_MASTER_KEY"`。用 `podman exec litellm printenv LITELLM_MASTER_KEY` 比對。如果你換過金鑰，請重啟 Proxy——它是在啟動時讀取的。 |
| OpenRouter 回傳 `404` / *"No endpoints found"* | 該模型 slug 已被上游淘汰。這裡曾經就因為裸的 `anthropic/claude-3.5-sonnet` slug 發生過一次。 | 查看即時目錄：`curl -s https://openrouter.ai/api/v1/models`。更新 `config/config.yaml` 中的 `model:` 欄位並重啟 Proxy（[7.8](#78-新增或變更模型)）。 |
| OpenRouter 回傳 `401`，或 *"insufficient credits"* | `OPENROUTER_API_KEY` 錯誤／已撤銷，或帳號沒有餘額。 | 到 <https://openrouter.ai/keys> 驗證金鑰並確認餘額。確認它有進到容器裡：`podman exec litellm printenv OPENROUTER_API_KEY \| cut -c1-8`。 |
| Postgres 資料目錄出現 `permission denied`（rootless） | 使用 bind 掛載時，容器內 postgres 的 UID（70）沒有對應到可寫入的主機 UID。 | 請使用兩條路徑本來就採用的**具名資料卷**，而不是 bind 掛載。如果你非用 bind 掛載不可，請在掛載參數加上 `:U`，或執行 `podman unshare chown -R 70:70 /path/to/dir`。 |
| 讀取 `config.yaml` 出現 `permission denied`，且 `ausearch -m avc -ts recent` 中有 SELinux `avc: denied` | 被掛載的檔案沒有容器用的 SELinux 標籤。 | 兩條路徑都已經以 `:Z` 掛載（`ro,Z`）。用 `ls -Z ~/.config/litellm/config.yaml` 確認標籤（預期為 `container_file_t`）。**絕對不要把一個帶 `:Z` 標籤的檔案分享給兩套堆疊**——`:Z` 會私有化地重新標記，會弄壞另一個使用者；若該檔案確實必須共用，請改用 `:z`。 |
| 連接埠 4000 出現 `bind: address already in use` | 有其他東西佔用該連接埠——常見的是殘留的容器，或同一台主機上 k3s 那一側的部署。 | 執行 `ss -ltnp \| grep :4000`（或 `sudo lsof -i :4000`）。把它釋放掉，或修改 `.env` 中的 `LITELLM_PORT`（路徑 A）／`litellm.container` 中的 `PublishPort=`（路徑 B）。 |
| 重開機後堆疊沒有回來（路徑 B） | linger 沒開，所以 systemd 使用者管理員從未在開機時啟動。 | 執行 `loginctl enable-linger "$USER"`，並以 `loginctl show-user "$USER" --property=Linger` 確認。同時確認單元有被 `default.target` 需要：`systemctl --user list-dependencies default.target \| grep -i litellm`。 |
| `Unit litellm-postgres.service not found` | Quadlet 產生器未能產出該單元——最常見的原因是在 Podman 4.x 上使用 `Notify=healthy`，而該版本的 `Notify=` 只接受 `true`/`false`。 | 執行產生器 dry-run（[4.5](#45-啟動任何東西之前先驗證單元檔)）並閱讀 stderr。在 Podman 4.x 上請把 `Notify=healthy` 註解掉；`litellm-wait-postgres.service` 仍會把關就緒狀態。然後執行 `systemctl --user daemon-reload`。 |
| 修改單元檔沒有任何效果 | 產生器尚未重新執行，或你編輯的是儲存庫中的副本而非已安裝的副本。 | 編輯 `~/.config/containers/systemd/<file>`，接著 `systemctl --user daemon-reload`，再重啟。重新執行 `./quadlet/install.sh` 會一次做完這三件事。 |
| `could not translate host name "litellm-postgres"` | 容器位於預設的 `podman` 網路上，而該網路沒有 DNS。 | 兩條路徑都建立自訂橋接網路（`litellm-network` / `litellm-net`）正是為了這個原因。用 `podman inspect litellm --format '{{json .NetworkSettings.Networks}}'` 與 `podman network ls` 檢查。確認 `litellm-network.service` 已啟動。 |
| journal 中出現 `Start operation timed out` | 冷啟動拉取映像檔的時間超過 `TimeoutStartSec`。 | 先預拉：`podman pull ghcr.io/berriai/litellm:v1.83.14-stable` 與 `podman pull docker.io/library/postgres:16-alpine`，然後再啟動。除非你傳入 `--no-pull`，否則 `install.sh` 會做這件事。 |
| 服務在少數幾次當機後就不再重試 | 觸及了 `StartLimitIntervalSec=300` 內的 `StartLimitBurst=5`——這是 `CrashLoopBackOff` 的 systemd 對應機制。 | 修好根本原因，然後執行 `systemctl --user reset-failed litellm.service && systemctl --user start litellm.service`。 |
| `--cpus` / `--memory` 看起來被忽略（rootless） | `cpu` / `cpuset` 控制器沒有委派給你的使用者 slice。 | 加上[1.2 節](#12-cgroups-v2)的 `Delegate=` drop-in，並登出所有工作階段。用 `podman inspect --format '{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}' litellm` 檢查實際生效的值。 |
| `podman-compose` 對某個必要變數報錯 | 某個 `${VAR:?message}` 防護被觸發。 | 讀那則訊息——它會指出是哪個變數，以及該如何產生值。修好 `.env`，並在 `up` 之前先重新執行 `podman-compose config >/dev/null`（請保留重導向，見 §3.4）。 |
| 在 Admin UI 刪掉的模型一直跑回來 | 它定義在 `config.yaml` 中，而 `model_list` 是與資料庫**合併**，不是被資料庫取代。 | 刪除或註解 `config.yaml` 中的該項目，然後重啟 Proxy（[7.8](#78-新增或變更模型)）。 |
| LiteLLM 容器內出現 `curl: command not found` | 該映像檔內含 Python，但沒有 curl。 | 使用 [6.4](#64-兩件不要做的事) 的 Python urllib 單行指令，或從主機端探測。 |

### 10.1 一般診斷掃描

```bash
podman ps -a --filter 'label=io.woowtech.stack=litellm-gw'
podman inspect --format '{{.State.Status}} {{.State.Health.Status}}' litellm litellm-postgres
podman logs --tail 200 litellm
podman logs --tail 200 litellm-postgres
podman network ls
podman volume ls

# Path B only
systemctl --user list-units --all 'litellm*'
systemctl --user status litellm.service --no-pager -l
journalctl --user -u 'litellm*' -b --no-pager | tail -n 200
/usr/lib/systemd/system-generators/podman-system-generator --user --dryrun
```

回報任何 issue 時，請附上該掃描的輸出（並先把機密資訊遮蔽掉）。

---

## 11. 解除安裝

### 11.1 路徑 A — compose

```bash
cd Woow_podman_litellm
export COMPOSE_PROJECT_NAME=litellm

# Stop and remove containers + network. THE pgdata VOLUME SURVIVES.
podman-compose down
```

> **`podman-compose down` 會保留你的資料。** 用 `podman-compose up -d`
> 把堆疊重新拉起來時，會沿用既有的 `pgdata` 資料卷，
> 所有虛擬金鑰、團隊、使用者與花費紀錄都完好無缺。

如果連資料也要銷毀：

```bash
# Back up first — this is irreversible
./scripts/backup.sh --mode compose

podman-compose down -v          # removes containers, network AND the pgdata volume
```

清掉任何殘留物：

```bash
podman volume ls
podman volume rm litellm_pgdata          # name depends on COMPOSE_PROJECT_NAME
podman network ls
podman network rm litellm_litellm-network
podman rmi ghcr.io/berriai/litellm:v1.83.14-stable docker.io/library/postgres:16-alpine

# Local files
rm -f .env                                # your secrets
rm -rf backups/                           # your backups — be sure
cd .. && rm -rf Woow_podman_litellm
```

### 11.2 路徑 B — Quadlet

```bash
./quadlet/uninstall.sh
```

預設執行是**非破壞性**的。它會依序停止服務
（`litellm.service` → `litellm-wait-postgres.service` → `litellm-postgres.service` →
`litellm-pgdata-volume.service` → 網路單元），移除四個 Quadlet 單元檔與那一個一般單元，
執行 `systemctl --user daemon-reload`，並移除 `litellm-net` podman 網路。

它**不會**動到 `litellm-pgdata` 資料卷、`~/.config/litellm/` 中的任何東西、
容器映像檔，或 linger 設定。

| 旗標 | 作用 |
|---|---|
| `--purge-data` | 一併刪除 `litellm-pgdata` 資料卷——**會摧毀資料庫** |
| `--purge-config` | 一併刪除 `~/.config/litellm/`（config.yaml **與** litellm.env） |
| `--purge-images` | 一併移除 LiteLLM 與 Postgres 映像檔 |
| `--disable-linger` | 一併執行 `loginctl disable-linger` |
| `-y`、`--yes` | 略過互動式提示 |
| `-h`、`--help` | 用法說明 |

破壞性旗標需要輸入確認字串：`--purge-data` 要求你輸入
`DELETE-LITELLM-DATA`，`--purge-config` 要求 `DELETE-LITELLM-CONFIG`。即使加了
`--yes`，`--purge-data` 仍會給你 10 秒的中止空窗。腳本在銷毀任何東西之前
會先印出備份提示：

```bash
podman exec litellm-postgres pg_dump -U litellm -d litellm > backup.sql
```

在你取得並驗證備份之後，完整移除的做法：

```bash
./scripts/backup.sh --mode quadlet --out ~/litellm-final-backup
./quadlet/uninstall.sh --purge-data --purge-config --purge-images --disable-linger
```

之後請驗證：

```bash
systemctl --user list-units 'litellm*'
podman ps -a --filter 'label=io.woowtech.stack=litellm-gw'
podman volume ls
podman network ls
```

這四項都應該顯示不出任何與這套堆疊相關的東西。

### 11.3 腳本不可用時的手動移除

```bash
systemctl --user stop litellm.service litellm-wait-postgres.service litellm-postgres.service

rm -f ~/.config/containers/systemd/litellm.container \
      ~/.config/containers/systemd/litellm-postgres.container \
      ~/.config/containers/systemd/litellm-pgdata.volume \
      ~/.config/containers/systemd/litellm.network \
      ~/.config/systemd/user/litellm-wait-postgres.service

systemctl --user daemon-reload

podman rm -f litellm litellm-postgres litellm-wait-postgres 2>/dev/null
podman network rm litellm-net 2>/dev/null

# Destructive — only after a verified backup
podman volume rm litellm-pgdata
rm -rf ~/.config/litellm
loginctl disable-linger "$USER"
```

### 11.4 日後重新安裝

> **請保留 `LITELLM_SALT_KEY`。** 如果你保住了 `litellm-pgdata` 資料卷
>（或一份備份封存檔），卻用*不同的* salt key 重新安裝，
> 那個資料庫裡的每一組供應商憑證都會永久無法解密。
> 在重新啟動堆疊之前，請把完全相同的值還原到 `litellm.env` / `.env` 中。
> `scripts/restore.sh` 會透過 manifest 指紋替你檢查這一點，並在確認不一致時拒絕還原。

---

## 相關儲存庫

- [`WOOWTECH/Woow_litellm_docker_compose`](https://github.com/WOOWTECH/Woow_litellm_docker_compose)
  — k3s / docker-compose 的姊妹部署。
- [`WOOWTECH/Woow_litellm_mcp_server`](https://github.com/WOOWTECH/Woow_litellm_mcp_server)
  — 用於管理執行中閘道的金鑰、團隊與花費的 MCP 管理主控台。
