# Tools Optimization Plan

## Current State Analysis

### Tool Count: 12 tools
1. get_vulnerability_findings
2. get_patch_compliance  
3. execute_patch_operation
4. get_command_status
5. create_maintenance_window
6. check_instance_health
7. check_cloudwatch_alarms
8. rollback_patches
9. get_resource_dependencies
10. get_maintenance_windows
11. generate_compliance_report
12. query_compliance_reports

### Helper Functions: 8
1. _get_compliance_bucket_name
2. _get_date_prefix
3. _calculate_sla_requirement
4. _normalize_environment
5. _fetch_reports_from_s3
6. _parse_report_metadata
7. _matches_filters
8. _calculate_report_statistics

---

## Reorganization Plan

### New Structure:
```
1. IMPORTS & SETUP
2. AWS CLIENT INITIALIZATION
3. PRIVATE HELPER FUNCTIONS (alphabetical)
   - _calculate_report_statistics
   - _calculate_sla_requirement
   - _fetch_reports_from_s3
   - _get_compliance_bucket_name
   - _get_date_prefix
   - _matches_filters
   - _normalize_environment
   - _parse_report_metadata
4. PUBLIC TOOLS (by category, then alphabetical)
   A. Vulnerability & Discovery
      - get_vulnerability_findings
   B. Patch Management
      - execute_patch_operation
      - get_command_status
      - get_patch_compliance
      - rollback_patches
   C. Health & Monitoring
      - check_cloudwatch_alarms
      - check_instance_health
      - get_resource_dependencies
   D. Maintenance Windows
      - create_maintenance_window
      - get_maintenance_windows
   E. Compliance & Reporting
      - generate_compliance_report
      - query_compliance_reports
```

---

## Optimization Opportunities

### 1. DRY Violations

**Issue**: Duplicate EC2 instance tag extraction
- `get_patch_compliance`: Extracts tags for SLA
- `get_resource_dependencies`: Extracts tags for business context
- `generate_compliance_report`: Extracts tags for metadata

**Fix**: Create `_get_instance_tags(instance_id)` helper

**Issue**: Duplicate environment normalization calls
- Multiple tools call `_normalize_environment()`

**Fix**: Already centralized ✅

**Issue**: Duplicate error handling patterns
```python
except Exception as e:
    logger.error(f"Error...")
    return {"error": str(e), ...}
```

**Fix**: Create error handling decorator or helper

---

### 2. KISS Violations

**Issue**: `get_resource_dependencies` is too complex (150+ lines)
- Queries Config
- Queries ELB (multiple calls)
- Calculates blast radius
- Generates recommendations

**Fix**: Break into smaller functions:
- `_get_config_data(resource_id)`
- `_get_load_balancer_info(relationships)`
- `_calculate_blast_radius(criticality, environment, load_balancers)`

**Issue**: `get_maintenance_windows` has complex instance validation logic

**Fix**: Extract `_validate_instance_coverage(window, instance_tags)` helper

---

### 3. Performance Optimizations

**Issue**: `check_instance_health` loops through instances one-by-one
```python
for instance_id in instance_ids:
    ssm_info = ssm_client.describe_instance_information(...)
```

**Fix**: Batch query with filters
```python
ssm_info = ssm_client.describe_instance_information(
    Filters=[{'Key': 'InstanceIds', 'Values': instance_ids}]
)
```

**Issue**: `check_service_status` makes N SSM send_command calls

**Fix**: Already batched ✅ (single command to all instances)

**Issue**: `get_resource_dependencies` makes multiple ELB calls in loop

**Fix**: Already optimized with batching ✅

---

### 4. Code Quality Issues

**Issue**: Inconsistent docstring format
- Some have detailed Args/Returns
- Some have single-line descriptions

**Fix**: Standardize to Google-style docstrings

**Issue**: Magic numbers
- `limit=50` in get_vulnerability_findings
- `limit=100` in check_cloudwatch_alarms
- `Duration=2` in create_maintenance_window

**Fix**: Define constants at top of file

**Issue**: Inconsistent return structures
- Some return `{"error": str(e)}`
- Some return `{"error": str(e), "instances": []}`

**Fix**: Standardize error response format

---

## Agent Optimization for use_aws

### Current Issue
Agent makes multiple `use_aws` calls when it could batch:
- Example: Dependency analysis makes ~15 separate ELB queries

### Proposed Solutions

**Option 1: Add batching guidance to supervisor prompt**
```
When using use_aws tool:
- Batch related queries when possible
- Use filters to reduce data transfer
- Prefer single calls with multiple resources over loops
```

**Option 2: Create specialized batch tools**
- `batch_describe_instances(instance_ids)`
- `batch_describe_target_groups(tg_arns)`

**Option 3: Add caching layer**
- Cache frequently accessed data (maintenance windows, instance tags)
- 5-10 minute TTL
- Reduces redundant API calls

**Recommendation**: Option 1 (prompt guidance) + Option 3 (caching for static data)

---

## Implementation Priority

### P0 (Must Do)
1. ✅ Reorganize file structure (helpers first, tools by category)
2. ✅ Add comprehensive comments to all functions
3. ✅ Standardize docstrings
4. ✅ Extract `_get_instance_tags()` helper (DRY)
5. ✅ Optimize `check_instance_health` batching

### P1 (Should Do)
6. Break down `get_resource_dependencies` complexity
7. Standardize error response format
8. Define constants for magic numbers
9. Add supervisor prompt guidance for use_aws batching

### P2 (Nice to Have)
10. Add caching layer for static data
11. Create error handling decorator
12. Add performance profiling to all tools

---

## Testing Plan

After each change:
1. Compile check: `python3 -m py_compile tools.py`
2. Deploy: `./deploy.sh agent`
3. Test affected workflows
4. Measure performance impact

---

## Estimated Effort

- Reorganization: 30 minutes
- DRY fixes: 45 minutes
- KISS refactoring: 60 minutes
- Performance optimization: 30 minutes
- Testing: 45 minutes

**Total: ~3.5 hours**

---

## Success Criteria

✅ All tools properly organized and commented
✅ No duplicate code (DRY violations eliminated)
✅ Complex functions broken down (KISS compliance)
✅ Performance improvements measurable
✅ All tests pass
✅ No regression in functionality

