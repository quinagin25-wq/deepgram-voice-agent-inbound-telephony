-- contractors table - single source of truth for the power dialer, Maya's
-- personalization, and the eventual site-quality scraper / multi-market
-- ingestion. Designed to be upsert-safe so importing a new market's list
-- never creates duplicate records for the same phone number.
--
-- Shared across all CO business entities (CO-002, CO-003, etc.) so
-- V.A.N.E.S.S.A. can see and reason across the whole portfolio, while
-- business_entity keeps each business's outreach list logically separate.

create table if not exists contractors (
    id uuid primary key default gen_random_uuid(),

    -- Which CO this contractor/lead belongs to. Required on every row.
    business_entity text not null default 'CO-003',

    -- Core identity (sourced from the original CSV import)
    phone text not null,                      -- E.164 format
    owner_name text,
    business_name text,
    email text,                               -- as originally provided/scraped

    -- Correction layer - never overwrites `email`, just supersedes it when present
    corrected_email text,

    -- Call/CRM status tracking
    status text default 'not_called',         -- not_called | dialed | dialed_manual | no_answer | booked | declined | callback_requested
    last_called_at timestamptz,
    call_notes text,

    -- Booking outcome (filled in once Maya successfully books via Calendly)
    booked_start_time timestamptz,
    calendly_reschedule_url text,
    calendly_cancel_url text,

    -- Site-quality scraper slot (Google Colab scraper, not yet built)
    has_website boolean,
    website_quality_score numeric,
    website_url text,

    -- Market/source tracking - supports pulling in new markets without collisions
    source_market text default 'NC',          -- e.g. 'NC', 'OK', etc.
    imported_at timestamptz default now(),
    updated_at timestamptz default now(),

    -- A contractor is unique per business entity - the same phone number
    -- could theoretically appear under two different CO's outreach lists
    -- without colliding.
    unique (business_entity, phone)
);

-- Keep updated_at current on every row change
create or replace function set_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists contractors_set_updated_at on contractors;
create trigger contractors_set_updated_at
    before update on contractors
    for each row
    execute function set_updated_at();

-- Index for V.A.N.E.S.S.A.'s cross-portfolio queries (e.g. "show me
-- everything in 'booked' status across all businesses" or "show me
-- CO-003's full list").
create index if not exists idx_contractors_business_entity on contractors (business_entity);
create index if not exists idx_contractors_status on contractors (status);

-- Upsert-safe import pattern for adding a new market's contractor list:
--
-- insert into contractors (business_entity, phone, owner_name, business_name, email, source_market)
-- values ('CO-003', '+19195551234', 'Jane Doe', 'Doe Construction', 'jane@doeconstruction.com', 'NC')
-- on conflict (business_entity, phone) do update set
--     owner_name = excluded.owner_name,
--     business_name = excluded.business_name,
--     email = excluded.email,
--     source_market = excluded.source_market,
--     updated_at = now();
--
-- This will never duplicate a contractor already in the table for that
-- business entity - if business_entity + phone match, it just refreshes
-- their info instead of erroring or creating a second row.
