# Demo Script Comparison: v1 vs v2

## Key Changes

### Scene Flow

| v1 | v2 | Why Changed |
|----|----|----|
| Scene 1: Discovery (vulnerabilities) | Scene 1: Compliance Context (instances) | **Safer** - Always works, no dependency on findings |
| Scene 2: Handle CVE (4 interactions) | Scene 2: SLA Analysis (hypothetical) | **Safer** - No execution risk, explains logic clearly |
| Scene 3: Lambda routing | Scene 3: Vulnerability check | **Better flow** - Check findings before patching |
| Scene 4: Compliance report | Scene 4: Lambda routing | **More coherent** - Routing demo still included |
| - | Scene 5: Patching (optional) | **Honest** - Only if findings exist, explains workflow |

---

## Detailed Comparison

### Opening
- **v1**: Same
- **v2**: Same
- **Change**: None

### Scene 1
- **v1**: "What critical vulnerabilities do we have?"
  - **Risk**: Fails if no findings
  - **Time**: 2 min
  
- **v2**: "Show me prod instances and their compliance frameworks"
  - **Benefit**: Always works, sets up SLA story
  - **Time**: 2 min
  - **Talking points**: Emphasizes compliance tags drive everything

### Scene 2
- **v1**: "Handle CVE-2025-38527 in dev"
  - **Risk**: Fake CVE, 4 interactions (handle → check status → check health → proceed → proceed)
  - **Problem**: Simulating workflow without actually waiting 10 minutes
  - **Time**: 3 min
  
- **v2**: "If we discover a CRITICAL vulnerability in prod, what's our SLA?"
  - **Benefit**: Hypothetical, no execution risk, explains decision logic
  - **Honest**: Shows SLA calculation without pretending to patch
  - **Time**: 2.5 min
  - **Talking points**: Policy-driven, JSON config, strictest SLA wins

### Scene 3
- **v1**: "List my Lambda functions"
  - **Purpose**: Show routing
  - **Time**: 2 min
  
- **v2**: "What vulnerabilities do we have in dev?"
  - **Purpose**: Check actual findings, show security specialist routing
  - **Fallback**: If no findings, explain what you'd see
  - **Time**: 2 min
  - **Better flow**: Leads naturally to patching (if findings exist)

### Scene 4
- **v1**: "Generate compliance report"
  - **Problem**: Disconnected - haven't patched anything yet
  - **Time**: 2 min
  
- **v2**: "List my Lambda functions"
  - **Purpose**: Show intelligent routing and fallback
  - **Time**: 1.5 min
  - **Benefit**: Quick win, always works

### Scene 5
- **v1**: Not present
  
- **v2**: "Patch [CVE-ID] in dev" (OPTIONAL)
  - **Condition**: Only if actual vulnerabilities found
  - **Honest**: Explains workflow without simulating
  - **Time**: 2 min
  - **Talking points**: Async design, health checks, approval gates

### Closing
- **v1**: Generic
- **v2**: Specific (mentions compliance frameworks, SLA policies, AWS services)
- **Change**: More concrete value proposition

---

## Risk Assessment

### v1 Risks
1. ❌ Assumes vulnerabilities exist (fails if none)
2. ❌ Uses fake CVE-2025-38527 (agent says "not found")
3. ❌ Simulates patching without waiting (audience questions authenticity)
4. ❌ Compliance report disconnected from workflow
5. ❌ 4 interactions in Scene 2 (timing risk)

### v2 Mitigations
1. ✅ Starts with always-working query (instances)
2. ✅ Uses hypothetical scenario for SLA (no execution needed)
3. ✅ Checks for real vulnerabilities, has fallback
4. ✅ Honest about timing ("In production, this takes 10 min...")
5. ✅ Removed disconnected compliance report scene

---

## Narrative Flow

### v1 Flow
```
Discovery → Patching (simulated) → Routing → Compliance
```
**Problem**: Patching scene is risky and simulated, compliance disconnected

### v2 Flow
```
Compliance Context → SLA Logic → Vulnerability Check → Routing → [Optional: Real Patching]
```
**Benefit**: Each scene builds on previous, safer, more coherent

---

## Time Breakdown

| Scene | v1 | v2 | Change |
|-------|----|----|--------|
| Opening | 0.5 min | 0.5 min | Same |
| Scene 1 | 2 min | 2 min | Same |
| Scene 2 | 3 min | 2.5 min | -0.5 min |
| Scene 3 | 2 min | 2 min | Same |
| Scene 4 | 2 min | 1.5 min | -0.5 min |
| Scene 5 | - | 2 min (optional) | +2 min |
| Closing | 1 min | 1 min | Same |
| **Total** | **10.5 min** | **9.5-11.5 min** | Flexible |

---

## Audience Impact

### v1 Weaknesses
- "Did it actually patch?" (No, simulated)
- "Why is the CVE not found?" (Fake ID)
- "Why show compliance report if nothing was patched?" (Disconnected)

### v2 Strengths
- "How does SLA calculation work?" (Clearly explained)
- "What if we have no vulnerabilities?" (Handled gracefully)
- "Is this real or simulated?" (Honest about timing)
- "Can I customize SLA policies?" (Yes, JSON config)

---

## Recommendation

**Use v2** because:
1. **Safer**: Works even with no vulnerabilities
2. **More honest**: Explains workflows vs simulating
3. **Better story**: Compliance → SLA → Decision logic
4. **More compelling**: Policy-driven approach highlighted
5. **Flexible**: Optional patching scene based on environment

**Keep from v1**:
- Opening (strong pain point)
- Q&A prep (thorough)
- Backup plan (solid)
- Checklists (comprehensive)
