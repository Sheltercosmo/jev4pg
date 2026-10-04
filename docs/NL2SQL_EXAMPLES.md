# Natural language to SQL examples

These queries were generated with jev4pg 0.5.0 and JEV 1.13.0, then checked against the [tutorial data](../examples/nl2sql/README.md). Each proposal required user review.

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

| Quantity | Supplier |
| ---: | --- |
| 36 | Birch |
| 30 | Aster |
| 8 | Cedar |

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

| Student | Score |
| --- | ---: |
| Ada | 92 |
| Chen | 88 |
| Emil | 92 |

## Average quantity with missing data

> What is the average quantity delivered, ignoring unknown quantities?

```sql
SELECT
  AVG("r0"."quantity") AS "result_1"
FROM "deliveries" AS "r0"
WHERE
  NOT "r0"."quantity" IS NULL
```

Result: `14.8`.

## Try it

Import the [tutorial datasets](../examples/nl2sql/cases.json), select the relevant table and submit a question. See the [tutorial instructions](../examples/nl2sql/README.md) or the [Simplified Chinese example](zh/USER_GUIDE.md#sql-示例).
