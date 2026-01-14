#!/bin/bash
#
# ARM Diagnostics Script
# Checks for stale temp files, stuck jobs, and ARM health
#
# Usage:
#   ./arm-diagnostics.sh          # Read-only diagnostics
#   ./arm-diagnostics.sh --clean  # Clean up stale files (with confirmation)
#

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

CLEAN_MODE=false
if [[ "$1" == "--clean" ]]; then
    CLEAN_MODE=true
fi

echo "========================================"
echo "       ARM Diagnostics Report"
echo "========================================"
echo ""

# Track issues found
ISSUES_FOUND=0
ABCDE_FOLDERS=""
LEFTOVER_WAVS=""
RIP_IN_PROGRESS=false

# 1. Check for abcde temp folders
echo -e "${YELLOW}[1/7] Checking abcde temp folders...${NC}"
ABCDE_FOLDERS=$(find /home/arm -maxdepth 1 -type d -name "abcde.*" 2>/dev/null || true)
if [[ -n "$ABCDE_FOLDERS" ]]; then
    echo -e "  ${RED}FOUND:${NC} Leftover temp folders:"
    echo "$ABCDE_FOLDERS" | while read -r folder; do
        if [[ -n "$folder" ]]; then
            SIZE=$(du -sh "$folder" 2>/dev/null | cut -f1)
            echo "    - $folder ($SIZE)"
        fi
    done
    ISSUES_FOUND=$((ISSUES_FOUND + 1))
else
    echo -e "  ${GREEN}OK:${NC} No leftover temp folders"
fi
echo ""

# 2. Check for running rip processes
echo -e "${YELLOW}[2/7] Checking for active rip processes...${NC}"
RIP_PROCS=$(ps aux | grep -E "abcde|cdparanoia|flac.*encode" | grep -v grep || true)
if [[ -n "$RIP_PROCS" ]]; then
    echo -e "  ${YELLOW}ACTIVE:${NC} Rip in progress!"
    echo "$RIP_PROCS" | head -5
    RIP_IN_PROGRESS=true
else
    echo -e "  ${GREEN}OK:${NC} No rip processes running"
fi
echo ""

# 3. Check for leftover WAV files
echo -e "${YELLOW}[3/7] Checking for leftover WAV files...${NC}"
LEFTOVER_WAVS=$(find /home/arm -name "*.wav" -type f 2>/dev/null || true)
if [[ -n "$LEFTOVER_WAVS" ]]; then
    WAV_COUNT=$(echo "$LEFTOVER_WAVS" | wc -l)
    echo -e "  ${RED}FOUND:${NC} $WAV_COUNT leftover WAV file(s)"
    ISSUES_FOUND=$((ISSUES_FOUND + 1))
else
    echo -e "  ${GREEN}OK:${NC} No leftover WAV files"
fi
echo ""

# 4. Check database for stuck jobs
echo -e "${YELLOW}[4/7] Checking database for stuck jobs...${NC}"
STUCK_JOBS=$(python3 -c "
import sqlite3
conn = sqlite3.connect('/home/arm/db/arm.db')
cur = conn.cursor()
cur.execute(\"SELECT job_id, title, status FROM job WHERE status NOT IN ('success', 'fail')\")
for row in cur.fetchall():
    print(f'  Job #{row[0]}: {row[1]} - status: {row[2]}')
conn.close()
" 2>/dev/null || echo "  Could not read database")

if [[ -z "$STUCK_JOBS" || "$STUCK_JOBS" == *"Could not"* ]]; then
    if [[ "$STUCK_JOBS" == *"Could not"* ]]; then
        echo -e "  ${YELLOW}WARN:${NC} $STUCK_JOBS"
    else
        echo -e "  ${GREEN}OK:${NC} No stuck jobs in database"
    fi
else
    echo -e "  ${RED}FOUND:${NC} Stuck jobs:"
    echo "$STUCK_JOBS"
    ISSUES_FOUND=$((ISSUES_FOUND + 1))
fi
echo ""

# 5. Check lock files
echo -e "${YELLOW}[5/7] Checking for stale lock files...${NC}"
LOCK_FILES=$(find /home/arm -name "*.lock" -o -name "*.pid" 2>/dev/null | head -10 || true)
if [[ -n "$LOCK_FILES" ]]; then
    echo -e "  ${YELLOW}FOUND:${NC} Lock/PID files:"
    echo "$LOCK_FILES"
else
    echo -e "  ${GREEN}OK:${NC} No stale lock files"
fi
echo ""

# 6. Check CD drive status
echo -e "${YELLOW}[6/7] Checking CD drive status...${NC}"
if [[ -e /dev/sr0 ]]; then
    DISC_INFO=$(blkid /dev/sr0 2>&1 || true)
    if [[ "$DISC_INFO" == *"No such"* || -z "$DISC_INFO" ]]; then
        echo -e "  ${GREEN}OK:${NC} Drive ready, no disc inserted"
    else
        echo -e "  ${YELLOW}INFO:${NC} Disc detected in drive"
    fi
else
    echo -e "  ${RED}ERROR:${NC} CD drive /dev/sr0 not found"
    ISSUES_FOUND=$((ISSUES_FOUND + 1))
fi
echo ""

# 7. Check raw/transcode directories
echo -e "${YELLOW}[7/7] Checking media directories...${NC}"
RAW_COUNT=$(find /home/arm/media/raw -type f 2>/dev/null | wc -l || echo "0")
TRANSCODE_COUNT=$(find /home/arm/media/transcode -type f 2>/dev/null | wc -l || echo "0")
if [[ "$RAW_COUNT" -gt 0 || "$TRANSCODE_COUNT" -gt 0 ]]; then
    echo -e "  ${YELLOW}INFO:${NC} Raw files: $RAW_COUNT, Transcode files: $TRANSCODE_COUNT"
else
    echo -e "  ${GREEN}OK:${NC} Media directories empty"
fi
echo ""

# Summary
echo "========================================"
echo "              Summary"
echo "========================================"
if [[ $ISSUES_FOUND -eq 0 ]]; then
    echo -e "${GREEN}All checks passed. ARM is in a clean state.${NC}"
else
    echo -e "${YELLOW}Found $ISSUES_FOUND potential issue(s).${NC}"
fi
echo ""

# Clean mode
if [[ "$CLEAN_MODE" == true ]]; then
    if [[ "$RIP_IN_PROGRESS" == true ]]; then
        echo -e "${RED}WARNING: A rip appears to be in progress!${NC}"
        echo "Cleaning now could corrupt the current rip."
        read -p "Are you SURE you want to continue? (type 'yes' to confirm): " CONFIRM
        if [[ "$CONFIRM" != "yes" ]]; then
            echo "Aborted."
            exit 1
        fi
    fi

    if [[ -n "$ABCDE_FOLDERS" || -n "$LEFTOVER_WAVS" ]]; then
        echo "The following will be deleted:"
        if [[ -n "$ABCDE_FOLDERS" ]]; then
            echo "  - abcde temp folders"
        fi
        if [[ -n "$LEFTOVER_WAVS" ]]; then
            echo "  - Leftover WAV files"
        fi
        echo ""
        read -p "Proceed with cleanup? (y/N): " CONFIRM
        if [[ "$CONFIRM" =~ ^[Yy]$ ]]; then
            if [[ -n "$ABCDE_FOLDERS" ]]; then
                echo "Removing abcde temp folders..."
                echo "$ABCDE_FOLDERS" | while read -r folder; do
                    if [[ -n "$folder" ]]; then
                        rm -rf "$folder"
                        echo "  Deleted: $folder"
                    fi
                done
            fi
            if [[ -n "$LEFTOVER_WAVS" ]]; then
                echo "Removing leftover WAV files..."
                echo "$LEFTOVER_WAVS" | while read -r wav; do
                    if [[ -n "$wav" ]]; then
                        rm -f "$wav"
                        echo "  Deleted: $wav"
                    fi
                done
            fi
            echo -e "${GREEN}Cleanup complete.${NC}"
        else
            echo "Cleanup cancelled."
        fi
    else
        echo "Nothing to clean up."
    fi
else
    if [[ $ISSUES_FOUND -gt 0 ]]; then
        echo "Run with --clean flag to clean up stale files:"
        echo "  docker exec -it arm-ripper /etc/arm/config/arm-diagnostics.sh --clean"
    fi
fi
echo ""
