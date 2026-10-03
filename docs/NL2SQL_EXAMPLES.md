# Natural language to SQL examples

These queries were generated and executed with jevsd-pg 0.5.0 and JEV 1.13.0. Each result matched the fixed tutorial reference. The application held these proposals for review; the evaluation executed them to check the answers. Generated SQL is shown unchanged.

## Total deliveries by supplier

> For each supplier, show the total quantity delivered, largest total first.

```sql
SELECT
  SUM("r0"."quantity") AS "result_1",
  "r0"."supplier" AS "result_2"
FROM "deliveries" AS "r0"
GROUP BY
  "r0"."supplier"
ORDER BY
  SUM("r0"."quantity") DESC NULLS LAST
```

Result:

```json
[
  {
    "result_1": 36,
    "result_2": "Birch"
  },
  {
    "result_1": 30,
    "result_2": "Aster"
  },
  {
    "result_1": 8,
    "result_2": "Cedar"
  }
]
```

## Filter and sort exam results

> Show the names and scores of students who scored at least 80, ordered by name.

```sql
SELECT
  "r0"."student" AS "result_1",
  "r0"."score" AS "result_2"
FROM "exams" AS "r0"
WHERE
  "r0"."score" >= 80
ORDER BY
  "r0"."student" ASC
```

Result:

```json
[
  {
    "result_1": "Ada",
    "result_2": 92
  },
  {
    "result_1": "Chen",
    "result_2": 88
  },
  {
    "result_1": "Emil",
    "result_2": 92
  }
]
```

## Average quantity with missing data

> What is the average quantity delivered, ignoring unknown quantities?

```sql
SELECT
  AVG("r0"."quantity") AS "result_1"
FROM "deliveries" AS "r0"
WHERE
  NOT "r0"."quantity" IS NULL
```

Result:

```json
[
  {
    "result_1": 14.8
  }
]
```

## Try the same data

The [tutorial package](../examples/nl2sql/README.md) includes both tables, the requests, expected results and a comparison runner. To use the web workspace, import the dataset objects from `cases.json`, then submit the matching request.

A [Simplified Chinese example](zh/USER_GUIDE.md#sql-示例) uses the same exam data. The paired schema check also recovered the total quantity after the delivery table and every column were renamed, using their descriptions. This is one checked rename, not a claim about all schemas.
