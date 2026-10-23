update wf_spaces set name = 'Geo Follow-Up', key = 'GF' where key = 'FU' and division_id in (select id::text from divisions where lower(name) = 'geo') returning name, key;
