#!/bin/bash
# Quick agent test harness — extracts clean answer text from agentcore invoke
# Usage: ./test_agent.sh "Your prompt here"

cd "$(dirname "$0")/../agent"
source ../venv/bin/activate

PROMPT="$1"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "PROMPT: $PROMPT"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

START=$(date +%s)

AWS_PROFILE=ri25-demo agentcore invoke "{\"prompt\": \"$PROMPT\"}" 2>&1 | python3 -c "
import sys, re, json
text = sys.stdin.read()
# Find the final message JSON block
matches = re.findall(r'\{\"message\":\s*\{\"role\":\s*\"assistant\".*?\}\}\}', text, re.DOTALL)
if matches:
    try:
        msg = json.loads(matches[-1])
        answer = msg['message']['content'][0]['text']
        # Strip answer tags
        answer = re.sub(r'</?answer>', '', answer).strip()
        print(answer)
    except:
        print('[PARSE ERROR] Could not extract answer')
        print(matches[-1][:500])
else:
    # Fallback: look for text between answer tags in raw output
    ans = re.findall(r'<answer>(.*?)</answer>', text, re.DOTALL)
    if ans:
        print(ans[-1].strip())
    else:
        print('[NO ANSWER FOUND]')
        print(text[-1000:])
"

END=$(date +%s)
echo ""
echo "⏱️  Duration: $((END - START))s"
echo ""
