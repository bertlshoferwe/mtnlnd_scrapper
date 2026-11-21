-- Vendors: a contact's alternate phone (a cell beside an office line, a second office), and
-- search that matches it too. Safe to run more than once.

alter table vendor_contacts add column if not exists alt_phone text;

create or replace function vendor_search(p_division text, p_words text[])
returns setof uuid language sql stable as $$
  select v.id
  from vendors v
  where v.deleted_at is null
    and (exists (select 1 from vendor_divisions d where d.vendor_id = v.id and d.division_id = p_division)
         or exists (select 1 from vendor_items i where i.vendor_id = v.id and i.division_id = p_division and i.active))
    and not exists (
      select 1 from unnest(coalesce(p_words, '{}')) w
      where not (
        lower(v.name) like '%' || w || '%'
        or lower(coalesce(v.website, '')) like '%' || w || '%'
        or exists (select 1 from vendor_items i
                   where i.vendor_id = v.id and i.division_id = p_division and i.active
                     and lower(i.name) like '%' || w || '%')
        or exists (select 1 from vendor_contacts c
                   where c.vendor_id = v.id and c.deleted_at is null
                     and (lower(c.name || ' ' || coalesce(c.title, '') || ' ' || coalesce(c.email, '') || ' ' || coalesce(c.phone, '') || ' ' || coalesce(c.alt_phone, '')) like '%' || w || '%'
                          or (c.nationwide and 'nationwide' like '%' || w || '%')
                          or exists (select 1 from vendor_contact_areas a
                                     where a.contact_id = c.id
                                       and (lower(a.state_code) = w or lower(coalesce(a.region_label, '')) like '%' || w || '%'))))
      )
    )
$$;
