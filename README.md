# Agentic AI Merchant Onboarding Automation

An end-to-end agentic onboarding system that replaces manual merchant onboarding with an automated, multi-step pipeline triggered by a CRM event and running through to go-live with a human approval checkpoint.

**What it does:**

- Triggers automatically on a Closed Won deal in the CRM
- Validates data quality; not just completeness, catches fake or malformed values the same as empty fields
- Parses menus from PDFs, Excel files and images via a vision model into a single structured schema
- Routes merchants through conditional paths based on whether they are a new restaurant or an existing branch
- Assigns onboarding managers by current workload
- Sends WhatsApp escalations to sales reps for incomplete data
- Emails the 3PL for hardware shipping
- Pauses for human approval before go-live
- Scores every completed step via an LLM-as-Judge layer before the human handoff
- Resumes from the exact node if interrupted; no restart required

**Built with:** LangGraph, Claude (Anthropic), HubSpot, Twilio, SQLite
