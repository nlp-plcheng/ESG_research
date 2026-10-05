# ESG 承諾驗證　人工複核平台（中文版）

Flask + SQLite 的小型網頁平台：標註者逐年檢查 pipeline 對企業 ESG 承諾的追蹤判定。
一條承諾＋它的逐年 AI 判定＝一道題，在同一頁作答；答案彙整成「糾錯紀錄」與
「逐年達成機率軌跡」兩種資料集。

平台只讀 pipeline 的輸出（`summary.json`）與報告書 PDF，不改動 pipeline 本身。
英文版（英文介面、西元年可選、與本版可同機並行）在 `../../english/annotation_platform/`。

## 1. 需要什麼

- Python 3.9+（virtualenv 即可，不需 root）；伺服器上有 `tmux`、`curl`。
- pipeline 產出與報告書，放成這個結構（路徑可改）：

```
<edition 資料夾>/                  例如 ESG_research/chinese/
  pdf/<公司>/<民國年>.pdf           報告書，一年一檔
  result/<公司>/summary.json       每家一份，openai_build_summary_json.py 的輸出
  annotation_platform/             本資料夾
```

`<公司>` 是任意資料夾名稱，會原樣顯示給標註者；`<民國年>` 必須與 `summary.json` 裡的
`year` 一致。平台只送「被引用的那幾頁」的節錄 PDF，不會把整本報告書傳給標註者。

資料放在別處：在 `env.prod.sh`（或 `env.test.sh`）設 `ESG_RESULT_DIR`、`ESG_PDF_DIR` 即可，
其他都不用改。選用：本資料夾放 `industry.json`（`{"<公司>": "<產業>"}`）會在題目標頭顯示產業。

## 2. 快速開始

```bash
cd annotation_platform
bash run.sh setup        # venv + 套件 + cloudflared（一次）
bash run.sh test         # 測試 profile：倒資料後在 http://127.0.0.1:8080/?pid=test01 起服務
bash run.sh wipe         # 測完洗掉測試 DB
```

`test` 用 `env.test.sh`（獨立 DB、不走 proxy）。在遠端伺服器上先轉 port：
`ssh -N -L 8080:localhost:8080 <user>@<server>`，再開印出來的網址。

預設值：port 8080、DB 在 `/var/tmp/esg_annotation/`、tmux session `esg`
（英文版分別是 8081、`/var/tmp/esg_annotation_en/`、`esg-en`）。`stop`／`restart_web.sh`
只會對「經核對確實屬於本實例」的程序送訊號（工作目錄＋port；pid 檔不單獨採信），
所以兩版可以在同一台機器並行。要換 port：改 `run.sh`、`restart_web.sh`、`launch.sh`
開頭的 `PORT=`；要換 tmux session 名稱：在 shell 先 `export ESG_SESSION=<名稱>`。

## 3. 正式設定

```bash
bash run.sh prod-init    # 由範本產生 env.prod.sh（密鑰自動填）並倒入題目
```

然後編輯 `env.prod.sh`，重點：

| 變數 | 意義 |
|---|---|
| `ESG_RESULT_DIR`、`ESG_PDF_DIR` | `summary.json` 與 PDF 的位置（預設 `../result`、`../pdf`） |
| `ESG_DB_PATH` | SQLite 檔——**只能放本機磁碟**，不能放 NFS |
| `ESG_PID_ALLOWLIST` | 逗號分隔的參與者代號；一人一條連結，代號就是唯一憑證。`bash add_pids.sh 6` 產生隨機代號 |
| `ESG_N_ANNOTATORS` | 標註人數。`>0` 等量分配（每人題數差 ≤1、難度平衡）；`0` 先到先做 |
| `ESG_REDUNDANCY` / `ESG_REDUNDANCY_CONTROL` | 每道異常題／對照題幾人（例如 3 / 1） |
| `ESG_CALIBRATION_N` | 所有人先做的共同校準題（預設 5；自動挑異常分數 `priority` 最高的前 N 題） |
| `ESG_GOLD_FILE` | 選用：改用自訂校準題清單——JSON 陣列，每項 `{"company","declared_year","commitment","expect"?}`，須對得上既有題目（見 `ingest.LoadGold`）；`expect` 用平台現行標籤，設了就會做對錯計分 |
| `ESG_TEST_PIDS` | 測試帳號（預設 `self-check`）：看全部題目、不佔名額、不進匯出 |
| `ESG_VOLUNTEER_MODE`、`ESG_ALLOW_LOCAL_PID`、`ESG_TASKS_PER_SESSION` | 志願者（`1`、`1`、`0`）或 Prolific（`0`、`0`、`10`＋completion code） |

`env.prod.sh` 絕對不要 commit 或分享（已在 `.gitignore`）：裡面有 secret key、後台金鑰
與參與者代號。

## 4. 啟動、拿連結、重啟

```bash
bash launch.sh           # 一鍵：補空密鑰、（沒有任何作答時）重建題庫、起服務、驗證白名單、印出每人連結
bash run.sh links        # 再印一次連結（ESG_N_ANNOTATORS>0 時只印前 N 條）
bash run.sh status       # 活著沒？
bash restart_web.sh      # 套用「設定」修改：遷移 DB、重選校準題、重算分配、重啟 gunicorn
bash reload_web.sh       # 換「程式／模板」：備份 DB、只新增必要欄位、重啟 gunicorn；
                         #   校準題、分配、既有作答都不重算也不修改
.venv/bin/python backup_db.py   # 隨時可跑：線上一致性快照（不能用 cp，DB 是 WAL 模式）
bash run.sh tunnel       # 只重開對外通道
bash run.sh export       # exports/esg_item.csv、esg_year.csv、esg_risk.csv
bash run.sh stop
```

後台：`<網址>/admin?key=<ESG_ADMIN_KEY>`（進度、覆蓋度、每人題數、「最新送出的作答」
可確認答案真的有存到、CSV 下載）。底下四個子頁都只讀、都要帶後台金鑰：
「每人統計」（`/admin/annotators`）把每個人的修正率、出處頁碼判讀、機率的四分位數、
每題分鐘數、證據點閱排在一起並附全體對照，用來抓「一路按正確沒在看」的情況，數字和標註者
自己看到的是同一個函式算的；「逐題結果」（`/admin/items`，`?item=<題號>` 只看一題）把每一題
每個人的答案並排，可只看有分歧的題；「校準題統計」（`/admin/calibration`）把共同題逐年並排：
多數決、一致率、預估的中位數與四分位距、每個人與多數的一致程度；「大家的星號、備註與疑問」
（`/admin/notes`）集中列出每個人在題目上留下的星號、備註、「不是承諾／沒有目標年份」的理由
與敘述修正，最新的在前。

### 對外網址

`run.sh serve` 用免費的 Cloudflare quick tunnel 把本機 port 開到外網（`tunnel_loop.sh`：
不需 root、不開 inbound port、掛了自動重開）。quick tunnel **每次啟動網址都會變**，
收案超過幾天就設一次固定連結頁：

1. GitHub 開一個 public repo（例如 `esg-link`，含 README）；Settings → Pages →
   Deploy from branch `main` / root。
2. 伺服器上 `git clone git@github.com:<you>/esg-link.git ~/esg-link`，確認 `git push`
   不用輸入密碼（SSH key，或有寫入權的 deploy key）。
3. `env.prod.sh` 加 `export ESG_LINK_REPO_DIR=$HOME/esg-link`、
   `export ESG_LINK_PAGE_URL=https://<you>.github.io/esg-link/`。
4. `bash run.sh tunnel`。之後 `links` 印出的就是 `https://<you>.github.io/esg-link/?pid=…`；
   轉址頁會帶著 query string 轉到目前的通道網址，網址一變就自動更新（`logs/publish.log`）。

那個 repo 只能放轉址頁——`publish_link.sh` 發現有其他檔案被追蹤、暫存、或出現在未推送
的歷史裡，就會拒絕推送。

機器重開後自動復活（不需 root）：`crontab -e` 加
`@reboot sleep 60 && cd /path/to/annotation_platform && bash run.sh serve >> logs/reboot.log 2>&1`。

## 5. 標註者看到什麼

同意書 → 說明 → 題目。每題：Q0「這是不是承諾」（是／不是承諾／沒有目標年份／不確定；
「沒有目標年份」給像承諾、但報告書沒寫要在哪一年做到的句子——無從逐年追蹤，和「不是承諾」
一樣在這裡結束，理由選填）＋「承諾真的在 AI 標的那一頁嗎」
（否 → 來源頁碼錯誤／找不到承諾；選「找不到承諾」本題也結束）；
每個追蹤年度：「AI 判定對嗎」
（錯 → 勾正確依據：引用的證據／線索／都不是，重判四類狀態，填頁碼）＋ 0–100 達成機率滑桿；
最後是年份範圍檢查與整體結論（三類）。年度逐一解鎖；AI 的逐年總覽與整體結論要等全部
年度答完才出現。全程自動儲存；送出過的題可回頭修改。Q0 填了修正後的敘述，頁首的承諾
就換成那個人修正的版本（原文留在下一行）。承諾下方可以替題目加星號（不確定、想回頭再看）
和寫自己的備註：兩者各自自動儲存、在「作答紀錄」看得到（可只列加星號的）、管理者也看得到，
不算作答內容（匯出在 `esg_item.csv` 的 `starred`／`note` 欄）。

標籤：逐年 **已達成／尚未達成／遠離目標／未提及**；整體 **已達成／未達成／未提及**。
pipeline 的「部分達成」逐年顯示為「尚未達成」、整體視為「未達成」。

每頁標題列的「查看統計」（另開分頁）＝ `/mystats`：標註者自己的作答統計——進度、修正 AI
的比率、AI 判定 vs 自己判定的對照表、預估達成率分佈、證據頁點開次數。純唯讀、只查自己的
worker_id，看不到別人的數字，作答中隨時打開都不影響填答。

## 6. 輸出

| 檔案 | 粒度 | 用途 |
|---|---|---|
| `esg_item.csv` | 一（題, 標註者）一列 | AI 與真人整體結論、承諾有效性、旗標、時間 |
| `esg_year.csv` | 一（題, 年, 標註者）一列 | 糾錯紀錄本體＋逐年 0–100 機率 |
| `esg_risk.csv` | 一題一列（多數決） | 特徵 → 機率軌跡 → 真人驗證後的結果 |

測試帳號不進三份檔案。校準題和一般題一樣都在資料集裡（用 `is_gold` 欄區分）——而且因為
不受 REDUNDANCY 限制、每個人都會答，它們的標註人數比一般題多，多數決也更穩。

## 7. 檔案

```
app.py            Flask：派題、單頁作答、草稿、修改、PDF 節錄、後台
common.py         標籤對應、頁碼解析、DB、遷移
config.py         全部參數（都可用 ESG_* 環境變數覆寫）
ingest.py         summary.json → DB、異常評分、校準題
allocate.py       等量分配
export.py         三份 CSV
reset.py          洗資料（單一標註員／全部作答／整個 DB）
schema.sql        資料表
run.sh            setup / test / prod-init / serve / tunnel / links / status / stop / export
launch.sh         一鍵開站；restart_web.sh 套用設定修改；reload_web.sh 只換程式；add_pids.sh 補代號
backup_db.py      線上一致性 DB 快照（部署前先跑）
tunnel_loop.sh    通道保活；publish_link.sh 更新固定連結頁
render_pages.py   （備用）頁面轉圖；gold_example.json 品質檢核題範例；industry.json 公司→產業
templates/、static/style.css
```
