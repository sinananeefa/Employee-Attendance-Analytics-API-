# REVIEW.md

## Scope

This review compares the backup implementation (`app/main_backup.py`) with the corrected implementation (`app/main.py`) against the attendance and analytics API requirements. Each finding describes the observed behavior, how to reproduce or identify it, and the correction made in the current implementation.

## Findings

| # | Location | Finding and symptom | Fix in current implementation |
|---|---|---|---|
| 1 | `explain_endpoint` — status parameter | The explain endpoint accepts arbitrary status strings, unlike the attendance listing endpoint. An unsupported status can produce an explain result for an invalid filter. | Restrict status to the supported attendance-status values. |
| 2 | `department_trend` — aggregation pipeline | The explain endpoint constructs a separate trend pipeline, so its execution plan may not match the actual trend endpoint. | Reuse the shared trend pipeline builder. |
| 3 | `department_trend` — present count | The original trend calculation counts `PRESENT` and `WFH` but omits `ON_DUTY`. This undercounts attendance when on-duty records exist. | Count all statuses defined by `PRESENT_STATUSES`. |
| 4 | `department_trend` — date series | The original implementation emits only dates with attendance logs. Dates without logs are missing from the response. | Use `$densify` to generate a continuous daily series. |
| 5 | `department_trend` — headcount | The original trend response does not provide daily headcount. | Calculate headcount from employees whose `joined_on` date is on or before each date. |
| 6 | `department_trend` — attendance rate | The original trend response does not calculate the required daily attendance rate. | Calculate the rate using daily present count and headcount, with the required working-day and zero-headcount handling. |
| 7 | `department_trend` — moving average | The original response does not include the seven-row moving average of non-null attendance rates. | Calculate `moving_avg_7d` with `$setWindowFields` over the current and previous six daily rows. |
| 8 | `department_trend` — department lookup | A department without matching employees returns an empty result rather than the required not-found response. | Return HTTP 404 when no employees match the department and date range. |
| 9 | `department_trend` — range validation | The original endpoint checks reversed date ranges but does not enforce the maximum 92-day range. | Reject ranges longer than 92 days with HTTP 422. |
| 10 | `department_trend` — employee population | The original employee lookup does not restrict the population to employees who joined by the end date. | Filter employees using `joined_on <= to_date` when building the trend population. |
| 11 | `department_trend` — response contract | The original trend response uses `present_days` and omits required fields such as `present_count`, `is_working_day`, and `attendance_rate`. | Return the fields required by the trend response contract. |

## Areas inspected

The following behavior was inspected and should not be reported as a defect without additional evidence:

- Employee-code uniqueness and duplicate employee handling.
- Unique attendance records per employee and date.
- Late-minute threshold and overtime calculation.
- Work-hour rounding and half-day threshold.
- Punch-in duplicate handling and punch-out validation.
- Partial regularization, no-change rejection, and concurrent-edit protection.
- Monthly attendance calculations and weekday handling.
- Department-summary handling of employees without attendance logs.
- Leaderboard ranking and ties at the requested limit.
- Pagination and the use of MongoDB indexes.

## Verification still required

- Test a department with employees but no attendance logs in the requested trend range. Confirm that the endpoint returns a complete daily series rather than an empty `items` list.
- Verify trend results against a known dataset, including weekends, `ON_DUTY`, employees joining mid-range, and days with no logs.
- Run the tests and inspect execution statistics on representative data to confirm index usage and avoid collection scans.
- Confirm that `main_backup.py` is the original starter implementation before treating all listed differences as historical defects.