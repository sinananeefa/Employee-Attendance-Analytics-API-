# DECISIONS.md

## 1. Indexes

I created a unique index on employee codes to prevent duplicate employees, and a unique compound index on employee code and date to prevent duplicate attendance records. The department and joining-date index supports department headcount queries. The date and employee-code index supports attendance filtering and sorting. The employee-code and punch-in-time index supports employee punch history. The date and status index supports attendance queries filtered by status. I would avoid adding indexes without checking their query benefit because every extra index increases storage and write cost.

## 2. Punch-in race

If two identical punch-in requests arrive together, both may initially find no attendance record. MongoDB's unique employee/date index allows only one insert to succeed. The other request receives a duplicate-key error, which the API handles as a conflict instead of creating a second record.

## 3. Leaderboard ties

The leaderboard uses MongoDB's `$rank` based on total late minutes. Employees tied at the cutoff share the same rank, so all employees whose rank is within the limit are returned. The final sort makes the output order predictable.

## 4. Headcount

The department summary starts from employees, not attendance logs. It looks up matching logs while preserving employees with no matches, so employees with zero logs can still be counted.

## 5. Scaling

For 100 times more data, I would review explain execution statistics on representative large datasets, measure query latency, and tune indexes and aggregation pipelines based on the actual workload.