#!/bin/bash
# Post-rip fingerprint tagger for ARM
# Called by ARM with: $1=title, $2=body

SCRIPT="/etc/arm/config/fingerprint_tagger.py"
LOG_DIR="/home/arm/logs"
TAGGER_LOG="$LOG_DIR/fingerprint_tagger.log"

# ARM log format
arm_log() {
    local msg="$1"
    local timestamp=$(date "+%m-%d-%Y %H:%M:%S")
    echo "[$timestamp] INFO FINGERPRINT: $msg"
}

# Find the most recently modified job log (exclude arm.log and fingerprint_tagger.log)
find_job_log() {
    for f in $(ls -t "$LOG_DIR"/*.log 2>/dev/null); do
        case "$f" in
            */arm.log|*/fingerprint_tagger.log) continue ;;
            *) echo "$f"; return ;;
        esac
    done
}

# Start logging
arm_log "Post-rip hook triggered" >> "$TAGGER_LOG"
arm_log "Title: $1" >> "$TAGGER_LOG"

# Find current job's log
JOB_LOG=$(find_job_log)
arm_log "Job log: $JOB_LOG" >> "$TAGGER_LOG"

if [ -n "$JOB_LOG" ] && [ -f "$JOB_LOG" ]; then
    arm_log "========== FINGERPRINT TAGGER ==========" >> "$JOB_LOG"
fi

# Only process if Unknown Artist folder exists
if [ -d "/home/arm/music/Unknown Artist" ] && [ "$(ls -A /home/arm/music/Unknown\ Artist 2>/dev/null)" ]; then
    MSG="Found Unknown Artist folder, running fingerprint tagger..."
    arm_log "$MSG" >> "$TAGGER_LOG"
    [ -n "$JOB_LOG" ] && arm_log "$MSG" >> "$JOB_LOG"
    
    # Run tagger and capture output
    OUTPUT=$(python3 "$SCRIPT" --apply 2>&1)
    EXIT_CODE=$?
    
    # Log output
    echo "$OUTPUT" >> "$TAGGER_LOG"
    [ -n "$JOB_LOG" ] && echo "$OUTPUT" >> "$JOB_LOG"
    
    MSG="Fingerprint tagger completed (exit: $EXIT_CODE)"
    arm_log "$MSG" >> "$TAGGER_LOG"
    [ -n "$JOB_LOG" ] && arm_log "$MSG" >> "$JOB_LOG"
    [ -n "$JOB_LOG" ] && arm_log "=========================================" >> "$JOB_LOG"
else
    MSG="No Unknown Artist folder, skipping fingerprint check"
    arm_log "$MSG" >> "$TAGGER_LOG"
    [ -n "$JOB_LOG" ] && arm_log "$MSG" >> "$JOB_LOG"
    [ -n "$JOB_LOG" ] && arm_log "=========================================" >> "$JOB_LOG"
fi
