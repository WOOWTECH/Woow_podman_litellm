# WOOW LiteLLM Podman 版

[English](README.md)

本套件以 rootless Podman Quadlet 執行 LiteLLM **1.104.0** 與 PostgreSQL **17.11**。代理映像由固定 digest 的上游 non-root 版本、HA add-on 0.1.15 的相容性及安全修補、PaaS Chart 0.3.16 的 Woow plugin 建置。來源與功能差異見[對齊紀錄](docs/alignment.md)。

模型、供應商憑證、虛擬金鑰與預算在 `/ui` 管理，設定檔不預載任何模型。`/woow/chatgpt` 支援 ChatGPT 多帳號瀏覽器或裝置登入；帳號資料存 PostgreSQL，運作中的 token 檔位於 tmpfs。安裝時不需先提供 OpenRouter 金鑰。

## 全新安裝

需要 Podman 4.9 以上、rootless user systemd、linger、約 4 GiB RAM，以及連到 GHCR 與 Docker Hub 的網路。

```bash
git clone https://github.com/WOOWTECH/Woow_podman_litellm.git
cd Woow_podman_litellm
scripts/install.sh --dry-run
scripts/install.sh
tests/smoke.sh
```

安裝程式在本機建置 `localhost/woow-litellm:1.104.0-0.3.16`，拉取固定 digest 的 PostgreSQL 映像，建立 Podman secrets，然後啟動服務。首次啟動依序建立 Woow 資料表與 salt 複本、執行 Prisma migration、處理舊管理員 session、從資料庫還原 ChatGPT token，再開放 4000 埠。前置步驟失敗時代理不會開放服務。

預設網址：`http://127.0.0.1:4000/ui`，帳號預設 `admin`，獨立的 UI 密碼存於 Podman secret `litellm-ui-password`；API master key 是另一個 secret。請勿把憑證貼到對話或工單。對外開放須另外配置 TLS 反向代理與存取控制。API 為 `/v1`，ChatGPT 帳號管理為 `/woow/chatgpt`。

每日備份計時器在 UTC 19:00 執行 `scripts/backup.sh --daily`，保留七份每日備份；手動備份與升級前備份不會被這個保留規則刪除。備份包含 salt key，請另存於主機之外。

## 升級與資料保護

```bash
scripts/backup.sh
scripts/upgrade.sh
scripts/restore.sh <備份目錄> --with-secrets
systemctl --user status litellm litellm-postgres litellm-backup.timer
```

舊的 PostgreSQL **16** 資料卷不能直接由 PostgreSQL **17** 開啟，安裝程式會拒絕。請先用舊版本匯出、檢查備份，再於另一台主機或獨立 Podman 使用者建立 PG17，帶原有 `LITELLM_SALT_KEY` 還原後切換流量；詳見[遷移說明](docs/alignment.md#postgresql-16-to-17)。

PaaS 的租戶方案、Kubernetes NetworkPolicy、Cloudflare 入口及獨立管理 MCP 服務是平台功能，本套件不會自動建立。此 Podman 版保留 LiteLLM 本身支援的供應商與 MCP 設定能力。

本機映像建置、plugin 雜湊及 Quadlet 已驗證；是否正式部署成功，以目標主機的 `scripts/install.sh` 與 `tests/smoke.sh` 結果為準。Smoke test 不呼叫付費模型。
