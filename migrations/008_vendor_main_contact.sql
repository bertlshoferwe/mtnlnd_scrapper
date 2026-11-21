-- Vendors: each vendor's main contact (the ★ — who to call first there). One
-- per vendor; it clears itself if that contact is deleted. Safe to run more than once.

alter table vendors add column if not exists primary_contact_id uuid references vendor_contacts(id) on delete set null;
