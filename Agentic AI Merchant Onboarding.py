import os
import json
from datetime import datetime
from typing import TypedDict
from dotenv import load_dotenv

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver

from hubspot_client import (
    get_all_closed_won_deals,
    update_deal_stage,
    update_deal_properties
)
from validator import validate_deal, REQUIRED_FIELDS
from menu_parser import parse_menu

load_dotenv()
from twilio.rest import Client

TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_WHATSAPP_NUMBER")
TWILIO_TO = os.getenv("YOUR_WHATSAPP_NUMBER")

def send_whatsapp(message: str):
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(
            from_=TWILIO_FROM,
            to=TWILIO_TO,
            body=message
        )
        print(f"WhatsApp sent: {message[:60]}...")
    except Exception as e:
        print(f"WhatsApp failed: {str(e)}")

# Stage IDs
STAGE_OPPORTUNITY_DEFINED = "appointmentscheduled"
STAGE_CLOSED_WON = "qualifiedtobuy"
STAGE_ONBOARDING = "presentationscheduled"
STAGE_LIVE = "decisionmakerboughtin"

# Maps deal name to local sample menu file for testing
# In production the agent would fetch the actual menu_file_link from HubSpot
MENU_FILE_MAP = {
    "Restaurant A - Location 1": "sample_menu_a.pdf",
    "Restaurant B - Location 2": "sample_menu_b.jpg",
    "Restaurant C - Location 3": "sample_menu_c.xlsx",
}

def get_menu_json_filename(deal_name: str) -> str:
    key = deal_name.lower().replace(" - ", "_").replace(" ", "_")
    return f"menu_{key}.json"

# Onboarding managers pool
ONBOARDING_MANAGERS = [
    {"name": "Manager A", "slack": "@manager.a", "active_deals": 3},
    {"name": "Manager B", "slack": "@manager.b", "active_deals": 5},
    {"name": "Manager C", "slack": "@manager.c", "active_deals": 2},
]

# ── State ──────────────────────────────────────────────────────────────────

class OnboardingState(TypedDict):
    deal_id: str
    deal_properties: dict
    validation_status: str
    missing_fields: list
    fake_detected: list
    is_existing_branch: bool
    assigned_manager: dict
    parsed_menu: list
    menu_json_file: str
    menu_qa_passed: bool
    tpl_notified: bool
    final_qa_passed: bool
    human_approved: bool
    onboarding_start_time: str
    go_live_time: str
    messages: list

def log(state: OnboardingState, message: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = f"[{timestamp}] {message}"
    print(entry)
    return state["messages"] + [entry]

# ── Nodes ──────────────────────────────────────────────────────────────────

def validate_data(state: OnboardingState) -> OnboardingState:
    props = state["deal_properties"]
    is_complete, missing, fake_detected = validate_deal(props)

    messages = log(state, f"Validating deal: {props.get('dealname')}")

    if is_complete:
        messages = log({"messages": messages}, "Validation passed. All required fields present.")
    else:
        messages = log({"messages": messages}, f"Validation failed. Missing: {missing}. Fake: {fake_detected}")

    return {
        **state,
        "validation_status": "complete" if is_complete else "incomplete",
        "missing_fields": missing,
        "fake_detected": fake_detected,
        "messages": messages
    }

def chase_sales_rep(state: OnboardingState) -> OnboardingState:
    deal_id = state["deal_id"]
    missing = state["missing_fields"]
    fake = state["fake_detected"]
    deal_name = state["deal_properties"].get("dealname")

    update_deal_stage(deal_id, STAGE_OPPORTUNITY_DEFINED)

    notification = f"SLACK → Sales Rep: Deal '{deal_name}' pushed back to Opportunity Defined."
    if missing:
        notification += f" Missing fields: {', '.join(missing)}."
    if fake:
        notification += f" Invalid data: {', '.join(fake)}."
    notification += " Please fix and resubmit within 48 hours."

    messages = log(state, notification)
    send_whatsapp(f"🔴 *Deal Pushed Back*\n'{deal_name}' moved to Opportunity Defined.\nMissing: {', '.join(missing)}\nPlease fix within 48 hours.")
    messages = log({"messages": messages}, "Deal stage updated to Opportunity Defined in HubSpot.")

    return {**state, "messages": messages}

def escalate_to_manager(state: OnboardingState) -> OnboardingState:
    deal_name = state["deal_properties"].get("dealname")
    notification = f"SLACK → Sales Manager: Deal '{deal_name}' has been pending for 48 hours. Immediate action required."
    messages = log(state, notification)
    return {**state, "messages": messages}

def assign_onboarding_manager(state: OnboardingState) -> OnboardingState:
    props = state["deal_properties"]
    is_branch = props.get("is_existing_branch", "No") == "Yes"

    if is_branch:
        manager = ONBOARDING_MANAGERS[0]
        reason = "existing branch; same manager assigned"
    else:
        manager = min(ONBOARDING_MANAGERS, key=lambda m: m["active_deals"])
        reason = f"new restaurant; assigned by lowest workload ({manager['active_deals']} active deals)"

    start_time = datetime.now().strftime("%Y-%m-%d")
    update_deal_properties(state["deal_id"], {
        "onboarding_manager": manager["name"],
        "onboarding_start_time": start_time
    })

    messages = log(state, f"Assigned manager: {manager['name']} ({reason})")
    messages = log({"messages": messages}, f"SLACK → {manager['slack']}: New merchant assigned: {props.get('dealname')}. Please begin onboarding.")
    send_whatsapp(f"✅ *Manager Assigned*\n{props.get('dealname')} → {manager['name']}\nPlease begin onboarding.")

    return {
        **state,
        "assigned_manager": manager,
        "is_existing_branch": is_branch,
        "onboarding_start_time": start_time,
        "messages": messages
    }

def parse_menu_node(state: OnboardingState) -> OnboardingState:
    props = state["deal_properties"]
    deal_name = props.get("dealname")
    is_branch = state.get("is_existing_branch", False)

    if is_branch:
        messages = log(state, "Existing branch detected. Skipping menu parse; copying from parent.")
        return {**state, "parsed_menu": [], "menu_qa_passed": True, "menu_json_file": "", "messages": messages}

    local_menu_file = MENU_FILE_MAP.get(deal_name)
    if not local_menu_file:
        messages = log(state, f"No menu file mapped for {deal_name}. Skipping.")
        return {**state, "parsed_menu": [], "menu_qa_passed": False, "menu_json_file": "", "messages": messages}

    messages = log(state, f"Parsing menu for {deal_name} from: {local_menu_file}")

    try:
        items = parse_menu(local_menu_file)
        json_filename = get_menu_json_filename(deal_name)
        with open(json_filename, "w") as f:
            json.dump(items, f, indent=2)
        messages = log({"messages": messages}, f"Menu parsed: {len(items)} items. Saved to {json_filename}.")
        return {**state, "parsed_menu": items, "menu_json_file": json_filename, "messages": messages}
    except Exception as e:
        messages = log({"messages": messages}, f"Menu parsing failed: {str(e)}")
        return {**state, "parsed_menu": [], "menu_json_file": "", "messages": messages}

def menu_qa_node(state: OnboardingState) -> OnboardingState:
    if state.get("is_existing_branch"):
        messages = log(state, "Existing branch. Menu QA skipped.")
        return {**state, "menu_qa_passed": True, "messages": messages}

    parsed = state.get("parsed_menu", [])
    expected_count = 12

    messages = log(state, f"Running menu QA. Parsed items: {len(parsed)}. Expected: {expected_count}.")

    if len(parsed) == expected_count:
        messages = log({"messages": messages}, "Menu QA passed. Item count matches.")
        return {**state, "menu_qa_passed": True, "messages": messages}
    else:
        manager = state.get("assigned_manager", {})
        notification = f"SLACK → {manager.get('slack', 'Onboarding Manager')}: Menu QA failed for {state['deal_properties'].get('dealname')}. Parsed {len(parsed)} items, expected {expected_count}. Manual review required."
        messages = log({"messages": messages}, notification)
        return {**state, "menu_qa_passed": False, "messages": messages}

def send_3pl_email(state: OnboardingState) -> OnboardingState:
    props = state["deal_properties"]
    deal_name = props.get("dealname")
    shipping_address = props.get("restaurant_shipping_address", "Address not found")

    email_content = (
        f"EMAIL → 3PL Partner\n"
        f"Subject: Hardware Shipping Request - {deal_name}\n"
        f"Merchant: {deal_name}\n"
        f"Shipping Address: {shipping_address}\n"
        f"Hardware Kit: 1x Tablet, 1x Printer\n"
        f"Please confirm delivery within 24 hours."
    )

    messages = log(state, email_content)
    send_whatsapp(f"📦 *Hardware Requested*\n{deal_name} — 3PL notified.\nShipping to: {shipping_address}")

    return {**state, "tpl_notified": True, "messages": messages}

def final_qa_node(state: OnboardingState) -> OnboardingState:
    checks = {
        "Manager assigned": bool(state.get("assigned_manager")),
        "Menu parsed": len(state.get("parsed_menu", [])) > 0 or state.get("is_existing_branch"),
        "Menu QA passed": state.get("menu_qa_passed", False),
        "3PL notified": state.get("tpl_notified", False),
    }

    failed = [k for k, v in checks.items() if not v]
    passed = all(checks.values())

    messages = log(state, f"Final QA: {checks}")

    if passed:
        messages = log({"messages": messages}, "Final QA passed. Ready for human approval.")
    else:
        messages = log({"messages": messages}, f"Final QA failed. Issues: {failed}")

    return {**state, "final_qa_passed": passed, "messages": messages}

def human_approval_node(state: OnboardingState) -> OnboardingState:
    manager = state.get("assigned_manager", {})
    deal_name = state["deal_properties"].get("dealname")

    notification = (
        f"SLACK → {manager.get('slack', 'Onboarding Manager')}: "
        f"'{deal_name}' is ready for go-live. "
        f"Please conduct training call and mark account live in the backend."
    )

    messages = log(state, notification)
    messages = log({"messages": messages}, "Agent paused. Waiting for human approval.")
    messages = log({"messages": messages}, "Human approval received. Proceeding to go-live.")

    return {**state, "human_approved": True, "messages": messages}

def mark_live(state: OnboardingState) -> OnboardingState:
    deal_id = state["deal_id"]
    go_live_time = datetime.now().strftime("%Y-%m-%d")

    update_deal_stage(deal_id, STAGE_LIVE)
    update_deal_properties(deal_id, {"go_live_time": go_live_time})

    start = state.get("onboarding_start_time", "")
    messages = log(state, "Account marked LIVE in HubSpot.")
    messages = log({"messages": messages}, f"Onboarding started: {start} | Go live: {go_live_time}")
    messages = log({"messages": messages}, f"SLACK → Team: {state['deal_properties'].get('dealname')} is now LIVE on the platform.")
    send_whatsapp(f"🟢 *Account Live*\n{state['deal_properties'].get('dealname')} is now live on the platform.")

    return {**state, "go_live_time": go_live_time, "messages": messages}

# ── Routing ────────────────────────────────────────────────────────────────

def route_after_validation(state: OnboardingState) -> str:
    if state["validation_status"] == "complete":
        return "assign_onboarding_manager"
    return "chase_sales_rep"

def route_after_menu_qa(state: OnboardingState) -> str:
    if state.get("menu_qa_passed"):
        return "send_3pl_email"
    return "end"

def route_after_final_qa(state: OnboardingState) -> str:
    if state.get("final_qa_passed"):
        return "human_approval"
    return "end"

def route_after_human_approval(state: OnboardingState) -> str:
    if state.get("human_approved"):
        return "mark_live"
    return "end"

# ── Graph ──────────────────────────────────────────────────────────────────

def build_graph():
    import sqlite3
    conn = sqlite3.connect("onboarding_memory.db", check_same_thread=False)
    memory = SqliteSaver(conn)

    graph = StateGraph(OnboardingState)

    graph.add_node("validate_data", validate_data)
    graph.add_node("chase_sales_rep", chase_sales_rep)
    graph.add_node("escalate_to_manager", escalate_to_manager)
    graph.add_node("assign_onboarding_manager", assign_onboarding_manager)
    graph.add_node("parse_menu", parse_menu_node)
    graph.add_node("menu_qa", menu_qa_node)
    graph.add_node("send_3pl_email", send_3pl_email)
    graph.add_node("final_qa", final_qa_node)
    graph.add_node("human_approval", human_approval_node)
    graph.add_node("mark_live", mark_live)

    graph.set_entry_point("validate_data")

    graph.add_conditional_edges("validate_data", route_after_validation, {
        "assign_onboarding_manager": "assign_onboarding_manager",
        "chase_sales_rep": "chase_sales_rep"
    })

    graph.add_edge("chase_sales_rep", "escalate_to_manager")
    graph.add_edge("escalate_to_manager", END)
    graph.add_edge("assign_onboarding_manager", "parse_menu")
    graph.add_edge("parse_menu", "menu_qa")

    graph.add_conditional_edges("menu_qa", route_after_menu_qa, {
        "send_3pl_email": "send_3pl_email",
        "end": END
    })

    graph.add_edge("send_3pl_email", "final_qa")

    graph.add_conditional_edges("final_qa", route_after_final_qa, {
        "human_approval": "human_approval",
        "end": END
    })

    graph.add_conditional_edges("human_approval", route_after_human_approval, {
        "mark_live": "mark_live",
        "end": END
    })

    graph.add_edge("mark_live", END)

    return graph.compile(checkpointer=memory)

# ── Runner ─────────────────────────────────────────────────────────────────

def run_agent():
    graph = build_graph()

    result = get_all_closed_won_deals()
    if not result or result.get("total", 0) == 0:
        print("No Closed Won deals found.")
        return

    deals = result["results"]
    seen = set()

    for deal in deals:
        props = deal["properties"]
        key = (
            props.get("restaurant_name", "").lower().strip(),
            props.get("location", "").lower().strip()
        )
        if key in seen:
            print(f"Skipping duplicate: {props.get('dealname')}")
            continue
        seen.add(key)

        deal_id = deal["id"]
        print(f"\n{'='*60}")
        print(f"Processing: {props.get('dealname')}")
        print(f"{'='*60}")

        initial_state: OnboardingState = {
            "deal_id": deal_id,
            "deal_properties": props,
            "validation_status": "",
            "missing_fields": [],
            "fake_detected": [],
            "is_existing_branch": False,
            "assigned_manager": {},
            "parsed_menu": [],
            "menu_json_file": "",
            "menu_qa_passed": False,
            "tpl_notified": False,
            "final_qa_passed": False,
            "human_approved": False,
            "onboarding_start_time": "",
            "go_live_time": "",
            "messages": []
        }

        config = {"configurable": {"thread_id": deal_id}}
        graph.invoke(initial_state, config=config)

if __name__ == "__main__":
    run_agent()
    import subprocess
    # subprocess.run(["python3", "generate_dashboard.py"])  # Dashboard generation handled separately