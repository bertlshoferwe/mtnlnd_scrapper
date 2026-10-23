update wf_spaces set name = 'Geo Bids', key = 'GB' where key = 'BW' and parent_id is null and division_id in (select id::text from divisions where lower(name) = 'geo') returning name, key;
