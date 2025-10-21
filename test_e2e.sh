#!/bin/bash
# E2E Test Script - DEMO_SCRIPT_v2 Scenarios

REPORT_FILE="e2e_test_report_$(date +%Y%m%d_%H%M%S).md"
CHAT_CLIENT="./chat.sh"

echo "# E2E Test Report - $(date)" > $REPORT_FILE
echo "" >> $REPORT_FILE
echo "## Test Configuration" >> $REPORT_FILE
echo "- Demo Script: DEMO_SCRIPT_v2.md" >> $REPORT_FILE
echo "- Test Date: $(date)" >> $REPORT_FILE
echo "- Agent: Reorganized structure (agents/, tools/, utils/)" >> $REPORT_FILE
echo "" >> $REPORT_FILE

# Function to test a scenario
test_scenario() {
    local scene_num=$1
    local scene_name=$2
    local prompt=$3
    local expected=$4
    
    echo "## Scene $scene_num: $scene_name" >> $REPORT_FILE
    echo "" >> $REPORT_FILE
    echo "**Prompt:** \`$prompt\`" >> $REPORT_FILE
    echo "" >> $REPORT_FILE
    echo "**Expected:** $expected" >> $REPORT_FILE
    echo "" >> $REPORT_FILE
    
    # Run test
    echo "Testing Scene $scene_num: $scene_name..."
    START=$(date +%s)
    
    # Use chat client with single message mode
    RESPONSE=$($CHAT_CLIENT -m "$prompt" 2>&1)
    EXIT_CODE=$?
    
    END=$(date +%s)
    DURATION=$((END - START))
    
    echo "**Duration:** ${DURATION}s" >> $REPORT_FILE
    echo "" >> $REPORT_FILE
    echo "**Exit Code:** $EXIT_CODE" >> $REPORT_FILE
    echo "" >> $REPORT_FILE
    echo "**Response:**" >> $REPORT_FILE
    echo '```' >> $REPORT_FILE
    echo "$RESPONSE" >> $REPORT_FILE
    echo '```' >> $REPORT_FILE
    echo "" >> $REPORT_FILE
    
    # Analysis
    echo "**Analysis:**" >> $REPORT_FILE
    
    # Check for routing indicator
    if echo "$RESPONSE" | grep -q "Consulting"; then
        AGENT=$(echo "$RESPONSE" | grep "Consulting" | head -1)
        echo "- ✅ Routing indicator present: $AGENT" >> $REPORT_FILE
    else
        echo "- ❌ No routing indicator found" >> $REPORT_FILE
    fi
    
    # Check for errors
    if echo "$RESPONSE" | grep -qi "error\|exception\|failed"; then
        echo "- ⚠️ Errors detected in response" >> $REPORT_FILE
    else
        echo "- ✅ No errors detected" >> $REPORT_FILE
    fi
    
    # Performance assessment
    if [ $DURATION -lt 30 ]; then
        echo "- ✅ Performance: Excellent (<30s)" >> $REPORT_FILE
    elif [ $DURATION -lt 60 ]; then
        echo "- ⚠️ Performance: Acceptable (30-60s)" >> $REPORT_FILE
    else
        echo "- ❌ Performance: Slow (>60s)" >> $REPORT_FILE
    fi
    
    echo "" >> $REPORT_FILE
    echo "---" >> $REPORT_FILE
    echo "" >> $REPORT_FILE
    
    # Wait between tests
    sleep 2
}

# Scene 1: Compliance Context
test_scenario "1" "Compliance Context" \
    "Show me prod instances and their compliance frameworks" \
    "5 prod instances with PCI-DSS, SOC2, HIPAA tags"

# Scene 2: SLA Analysis
test_scenario "2" "SLA-Aware Decision" \
    "Check the SLA requirement for prod instances with CRITICAL severity and show me the maintenance window timing" \
    "12h SLA (PCI-DSS), maintenance window timing, EMERGENCY/SCHEDULED decision"

# Scene 3: Vulnerability Check
test_scenario "3" "Vulnerability Analysis" \
    "What critical vulnerabilities do we have in dev environment?" \
    "List of vulnerabilities or 'no vulnerabilities found'"

# Scene 4: Simple Query
test_scenario "4" "Simple AWS Query" \
    "How many Lambda functions do I have?" \
    "Count of Lambda functions via use_aws"

# Scene 5: Patch Compliance
test_scenario "5" "Patch Compliance Check" \
    "Show me patch compliance status for staging instances" \
    "Patch states with missing/installed counts"

# Scene 6: Maintenance Windows
test_scenario "6" "Maintenance Windows" \
    "Show me maintenance windows for staging environment" \
    "Maintenance window schedule and next execution time"

echo "## Summary" >> $REPORT_FILE
echo "" >> $REPORT_FILE
echo "Test completed at $(date)" >> $REPORT_FILE
echo "" >> $REPORT_FILE

echo "✅ E2E Test Complete"
echo "Report saved to: $REPORT_FILE"
cat $REPORT_FILE
