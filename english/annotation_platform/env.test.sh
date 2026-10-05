# Testing profile: separate DB, short sessions, no proxy.
# Usage:  source env.test.sh
export ESG_DB_PATH=/var/tmp/esg_annotation_en/test.db
export ESG_SECRET_KEY=test-only-not-a-secret
export ESG_ADMIN_KEY=testadmin
export ESG_PROLIFIC_CODE=TESTCODE

# export ESG_RESULT_DIR=/path/to/result   # defaults to ../result
# export ESG_PDF_DIR=/path/to/pdf         # defaults to ../pdf
export ESG_YEAR_STYLE=roc

export ESG_VOLUNTEER_MODE=1
export ESG_TASKS_PER_SESSION=3      # reach the "done" page after 3 items
export ESG_REDUNDANCY=3
export ESG_ALLOW_LOCAL_PID=1        # ?pid=test01 entry
export ESG_BEHIND_PROXY=0
export ESG_PROGRESSIVE_REVEAL=1
export ESG_HOST=127.0.0.1
export ESG_PORT=${ESG_PORT:-8081}   # keeps a port exported before run.sh test

echo "test profile: DB=$ESG_DB_PATH  items/session=$ESG_TASKS_PER_SESSION"
