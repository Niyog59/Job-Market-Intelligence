-- Each day's inventory of all listings adds up to the postings known that day, and
-- grouped opportunities equal postings minus the extra members of active groups.
with inv as (
    select report_day, sum(source_records) as records, sum(opportunities) as opps
    from {{ ref('rpt_inventory_daily') }} where population = 'all' group by 1
),
pd as (
    select report_day, count(*) as records,
           count(*) - count(duplicate_group_id) + count(distinct duplicate_group_id) as opps
    from {{ ref('int_posting_days') }} group by 1
)
select * from inv join pd using (report_day)
where inv.records <> pd.records or inv.opps <> pd.opps
