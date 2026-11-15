# 抽大麻 · 交換禮物

一個活動、一個網址，手機與電腦都可使用。Flask + SQLite，圖片也存於資料庫；無第三方圖片追蹤。

## 使用流程

1. 負責人分享網站網址及 `INVITE_CODE` 活動邀請碼。
2. 參加者填姓名、性別、個人通行碼（至少 8 字）、商品名稱，並提供圖片或購買連結。備註填款式與規格，不要填姓名，以免洩漏答案。名字必須唯一，同名可加暱稱。
3. 截止前且配對前可以登入修改願望。截止時間預設 **2026/11/17 23:59，UTC+08:00（台灣時間）**。
4. 負責人開啟 `/admin`，以 `ADMIN_PASSWORD` 登入，確認所有人到齊，再按「開始配對」。不會因時間到而自動抽籤。
5. 至少 4 人才能配對。每人送一份、收一份，不抽到自己。每人固定三個選項：收禮者的願望 + 另外兩人的願望；排除送禮者自己的禮物，隨機打亂順序。
6. 參加者回到相同網址，以姓名和個人通行碼登入，看到收禮者姓名、性別與三個禮物選項，自行選購。只有圖片時，依圖片及備註購買。

配對與選項在伺服器一次儲存，重新整理或重啟不會重抽。配對後鎖定報名與編輯，無重抽按鈕。頁面及圖片網址不包含禮物主人的 ID 或正確答案。活動當天由收禮者揭曉，不提前提供對錯驗證。

## 部署到 Render

需要 GitHub 私人儲存庫，以及 **Render 付費 Web Service + 1 GB Persistent Disk**。工作區方案與個別服務費用是不同項目；建立服務前請確認 Render 顯示的實際費用。

1. 把本資料夾內容放到儲存庫根目錄（`app.py` 和 `render.yaml` 位於根目錄）。不要上傳本機 `data`、密碼或 `.env`。
2. Render 選 New → Blueprint，連接這個 GitHub 儲存庫，使用 `render.yaml`。
3. 設定 `ADMIN_PASSWORD`（至少 12 字、只給負責人）及 `INVITE_CODE`（活動共用邀請碼，建議至少 8 字）。`SECRET_KEY` 由 Render 產生。
4. 確認 Starter 服務與持久磁碟費用，再建立。部署成功後分享 Render 提供的 `https://…onrender.com` 網址。
5. 先用真實活動名單報名。正式服務不要放測試參加者，因目前不提供刪除／重設功能。

如果手動建立 Web Service：

| 項目 | 設定 |
|---|---|
| Runtime | Python |
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn 'app:create_app()' --workers 1 --threads 4 --bind 0.0.0.0:$PORT` |
| Health check | `/health` |
| Persistent Disk mount path | `/var/data` |
| Disk size | 1 GB |
| `DATA_DIR` | `/var/data` |
| `DEADLINE` | `2026-11-17T23:59:00+08:00` |
| `SECRET_KEY` | 隨機產生長字串，保持固定 |
| `ADMIN_PASSWORD` | 自訂至少 12 字 |
| `INVITE_CODE` | 自訂活動邀請碼 |

**不可直接用無磁碟的免費服務保存活動資料**：Render 一般檔案系統是暫存的，只有掛載磁碟路徑的資料會在重新部署後保留。單一服務實例使用 SQLite，不可水平擴展。

官方文件：[Flask 部署](https://render.com/docs/deploy-flask) · [持久磁碟](https://render.com/docs/disks) · [Blueprint 設定](https://render.com/docs/blueprint-spec)

## 本機啟動（PowerShell，Python 3.13）

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
$env:SECRET_KEY = (python -c "import secrets; print(secrets.token_hex(32))")
$env:ADMIN_PASSWORD = Read-Host '負責人密碼（至少 12 字）'
$env:INVITE_CODE = Read-Host '活動邀請碼'
.venv\Scripts\python app.py
```

開啟 `http://127.0.0.1:5000`。本機預設資料庫是 `data/gifts.sqlite3`。本機開發伺服器只綁定 127.0.0.1，不能當作分享給朋友的正式網址。

## 測試與限制

```powershell
.venv\Scripts\python -m unittest -v test_app
```

測試包含 10 人配對、不重複收禮、無自配、每人三個唯一選項且包含正確答案、圖片存取隔離、重啟持久性、重複／並發配對、截止鎖定、無效圖片與網址、CSRF 與負責人權限。

每個活動使用獨立部署。沒有 Email、找回通行碼、付款、代購、禮物猜測提交或自動揭曉功能。通行碼經雜湊儲存；忘記通行碼需由維護者另行處理。邀請碼避免陌生人隨意報名，但不驗證現實身分。兩位參加者若填相同商品，可能出現內容相似的選項，負責人應在開始前檢查。圖片會轉為 JPEG、縮小並移除原始 metadata。管理員可看到大家提交的願望；請只把負責人密碼交給活動負責人。
