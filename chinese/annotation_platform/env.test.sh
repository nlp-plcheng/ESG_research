# 測試 profile：獨立 DB、短 session、無等待秒數、每題都是 gold。
# 用法：source env.test.sh
export ESG_DB_PATH=/var/tmp/esg_annotation/test.db
export ESG_SECRET_KEY=test-only-not-a-secret
export ESG_ADMIN_KEY=testadmin
export ESG_PROLIFIC_CODE=TESTCODE

# export ESG_RESULT_DIR=/path/to/result   # 預設見 config.py 的 RESULT_DIR
# export ESG_PDF_DIR=/path/to/pdf         # 預設見 config.py 的 PDF_DIR

export ESG_TASKS_PER_SESSION=3      # 3 題就到「完成」頁
export ESG_REDUNDANCY=3
export ESG_GOLD_EVERY=1             # 每題都是 gold，讓 gold 計分有被跑到
export ESG_MIN_SECONDS_TASK=0       # 點過去不用等
export ESG_ALLOW_LOCAL_PID=1        # ?pid=test01 進入
export ESG_BEHIND_PROXY=0
export ESG_PROGRESSIVE_REVEAL=0     # 全部年度一次顯示；設 1 逐年解鎖
export ESG_HOST=127.0.0.1
export ESG_PORT=8080

echo "test profile: DB=$ESG_DB_PATH  tasks/session=$ESG_TASKS_PER_SESSION"
