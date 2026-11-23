"""
Synthetic data for the expense policy governance module (Halden Group, 2025).

Writes to data/raw/policy/:
  employees.csv           employees, their cost centre and approvers
  policy_rules.csv        limits by policy version (v1 from 1 Jan, v2 from 1 Jul)
  high_cost_cities.csv    cities with the higher hotel cap
  approval_matrix.csv     approver role by claim total
  exception_register.csv  pre-approved policy exceptions
  claims.csv              claim headers (submitted, approved, paid)
  claim_lines.csv         claim lines (expense date, category, merchant, amount, receipt ...)
  reviewer_actions.csv    what the reviewer does on each planted case (simulated)
and data/answer_key/expected_policy_cases.csv (used only by the tests).

Most claims are clean. The planted scenarios in docs/design.md (section 13) are added on top.
Deterministic: the same seed gives the same files.
"""
import csv
import random
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "data" / "raw" / "reference"
OUT = ROOT / "data" / "raw" / "policy"
KEY = ROOT / "data" / "answer_key" / "expected_policy_cases.csv"
rng = random.Random(2025)

HIGH_COST = ["London", "New York", "Singapore", "Mumbai"]
STANDARD = ["Bengaluru", "Pune", "Hyderabad", "Chennai", "Kolkata", "Delhi", "Jaipur", "Ahmedabad"]
MERCHANTS = {
    "Hotel": ["Lakeview Inn", "Metro Suites", "Harbour Hotel", "Grand Residency", "City Stay"],
    "Airfare": ["SkyJet Airways", "Blue Air", "Coastal Airlines"],
    "Meals": ["Spice Route", "Corner Cafe", "Green Leaf", "Urban Bistro", "Tiffin House", "Daily Bread"],
    "Ground transport": ["QuickCab", "Metro Taxi", "RideNow"],
    "Client entertainment": ["The Oak Room", "Saffron Grill", "Blue Ginger"],
    "Training": ["SkillHub", "Learnwise", "ProCert Institute"],
    "Other": ["Office Mart", "Print Point", "Courier Express"],
}


def read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write(path, rows, cols):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


HOLIDAYS = {date.fromisoformat(r["holiday_date"]) for r in read(REF / "holidays.csv")}


def is_working(d):
    return d.weekday() < 5 and d not in HOLIDAYS


def add_working_days(d, n):
    while n > 0:
        d += timedelta(days=1)
        if is_working(d):
            n -= 1
    return d


def next_working(d):
    while not is_working(d):
        d += timedelta(days=1)
    return d


# ------------------------------------------------------------------ organisation
depts = read(REF / "departments.csv")
ccs = [r for r in read(REF / "cost_centres.csv")
       if r["cc_code"].startswith("CC10") and r["cc_code"] not in ("CC1047", "CC1061")]
bus = sorted({d["bu_code"] for d in depts if d["bu_code"].startswith("BU0")})

employees, n = [], 0


def new_emp(cc, dept, bu, role, line_manager):
    global n
    n += 1
    e = {"employee_id": f"E{n:03d}", "cc_code": cc, "dept_code": dept, "bu_code": bu,
         "role": role, "line_manager_id": line_manager}
    employees.append(e)
    return e


fd = {}
for bu in bus:
    first_cc = next(c for c in ccs if next(d for d in depts if d["dept_code"] == c["dept_code"])["bu_code"] == bu)
    fd[bu] = new_emp(first_cc["cc_code"], first_cc["dept_code"], bu, "Finance director", "")
head = {}
for d in depts:
    if not d["bu_code"].startswith("BU0"):
        continue
    cc = next(c for c in ccs if c["dept_code"] == d["dept_code"])
    head[d["dept_code"]] = new_emp(cc["cc_code"], d["dept_code"], d["bu_code"], "Department head",
                                   fd[d["bu_code"]]["employee_id"])
managers = {}
for d_code, h in head.items():
    managers[d_code] = [new_emp(h["cc_code"], d_code, h["bu_code"], "Manager", h["employee_id"]) for _ in range(2)]
staff = []
for c in ccs:
    d_code = c["dept_code"]
    bu = head[d_code]["bu_code"]
    for _ in range(rng.choice([1, 2])):
        staff.append(new_emp(c["cc_code"], d_code, bu, "Staff", rng.choice(managers[d_code])["employee_id"]))
by_id = {e["employee_id"]: e for e in employees}
for e in employees:
    e["dept_head_id"] = head[e["dept_code"]]["employee_id"]
    e["finance_director_id"] = fd[e["bu_code"]]["employee_id"]
claimants = staff + [m for ms in managers.values() for m in ms]


def approver_for(emp, total):
    if total < 500:
        return emp["line_manager_id"]
    if total <= 2500:
        return emp["dept_head_id"] if emp["dept_head_id"] != emp["employee_id"] else emp["finance_director_id"]
    return emp["finance_director_id"]


# ------------------------------------------------------------------ policy data
policy_rules = []
for ver, vf, vt, hs, hh, days in [(1, "2025-01-01", "2025-06-30", 200, 300, 90), (2, "2025-07-01", "9999-12-31", 230, 325, 60)]:
    for key, val in [("hotel_standard_night", hs), ("hotel_high_cost_night", hh), ("meals_day", 75),
                     ("entertainment_per_attendee", 150), ("gift_per_item", 100), ("receipt_min", 25),
                     ("submit_within_days", days)]:
        policy_rules.append({"version": ver, "valid_from": vf, "valid_to": vt, "rule_key": key, "value": val})
approval_matrix = [{"min_total": 0, "max_total": 499.99, "approver_role": "Line manager"},
                   {"min_total": 500, "max_total": 2500, "approver_role": "Department head"},
                   {"min_total": 2500.01, "max_total": 999999, "approver_role": "Finance director"}]

# ------------------------------------------------------------------ claims
claims, lines, actions, key, exceptions = [], [], [], [], []
used = set()        # (employee, merchant, date) already used: avoids accidental duplicates and splits
meals_used = {}     # (employee, date) -> meal spend so far
cid, lid = 0, 0


def add_claim(emp, items, submit, approver=None, purpose="Client visit", planted=False):
    """items: list of dicts with expense_date, category, merchant, city, nights, attendees, amount,
    receipt (bool), travel_class, alcohol, client_present, pre_approval_ref."""
    global cid, lid
    cid += 1
    claim_id = f"C{cid:04d}"
    total = round(sum(i["amount"] for i in items), 2)
    approved = next_working(submit + timedelta(days=2))
    paid = next_working(approved + timedelta(days=3))
    if paid.year > 2025:
        return None
    claims.append({"claim_id": claim_id, "employee_id": emp["employee_id"], "submitted_date": submit,
                   "approved_by": approver or approver_for(emp, total), "approved_date": approved,
                   "paid_date": paid, "claim_total": f"{total:.2f}", "business_purpose": purpose})
    for i in items:
        lid += 1
        lines.append({"line_id": f"L{lid:05d}", "claim_id": claim_id, "expense_date": i["expense_date"],
                      "category": i["category"], "merchant": i["merchant"], "city": i.get("city", ""),
                      "nights": i.get("nights", ""), "attendees": i.get("attendees", ""),
                      "amount": f"{i['amount']:.2f}",
                      "receipt_id": f"R{lid:05d}" if i.get("receipt", True) else "",
                      "travel_class": i.get("travel_class", ""), "alcohol": i.get("alcohol", False),
                      "client_present": i.get("client_present", False),
                      "pre_approval_ref": i.get("pre_approval_ref", "")})
        if not planted:
            used.add((emp["employee_id"], i["merchant"], i["expense_date"]))
    return claim_id


def clean_item(emp, d):
    cat = rng.choices(["Hotel", "Airfare", "Meals", "Ground transport", "Client entertainment", "Training", "Other"],
                      [3, 2, 4, 4, 1, 1, 2])[0]
    for _ in range(20):
        m = rng.choice(MERCHANTS[cat])
        if (emp["employee_id"], m, d) not in used:
            break
    else:
        return None
    it = {"expense_date": d, "category": cat, "merchant": m}
    if cat == "Hotel":
        city = rng.choice(HIGH_COST if rng.random() < 0.2 else STANDARD)
        nights = rng.randint(1, 3)
        cap = 295 if city in HIGH_COST else 195
        it.update(city=city, nights=nights, amount=round(nights * rng.uniform(110, cap), 2))
    elif cat == "Airfare":
        it.update(travel_class="Economy", amount=round(rng.uniform(150, 900), 2))
    elif cat == "Meals":
        left = 72 - meals_used.get((emp["employee_id"], d), 0)
        if left < 15:
            return None
        amt = round(rng.uniform(12, left), 2)
        meals_used[(emp["employee_id"], d)] = meals_used.get((emp["employee_id"], d), 0) + amt
        it.update(amount=amt)
    elif cat == "Ground transport":
        it.update(amount=round(rng.uniform(8, 80), 2))
        if it["amount"] < 25 and rng.random() < 0.5:
            it["receipt"] = False          # receipts are optional under $25
    elif cat == "Client entertainment":
        att = rng.randint(2, 5)
        it.update(attendees=att, client_present=True, amount=round(att * rng.uniform(60, 145), 2))
    elif cat == "Training":
        it.update(amount=round(rng.uniform(100, 1500), 2))
    else:
        it.update(amount=round(rng.uniform(10, 120), 2))
    return it


def month_days(m):
    d = date(2025, m, 1)
    out = []
    while d.month == m:
        out.append(d)
        d += timedelta(days=1)
    return out


for m in range(1, 13):
    for _ in range(40):
        emp = rng.choice(claimants)
        d = rng.choice([x for x in month_days(m) if x.day <= 20])
        items = [it for it in (clean_item(emp, d + timedelta(days=k)) for k in range(rng.randint(1, 3))) if it]
        if items:
            add_claim(emp, items, next_working(max(i["expense_date"] for i in items) + timedelta(days=rng.randint(1, 20))))

# ------------------------------------------------------------------ planted scenarios
planted_emps = rng.sample(staff, 20)
P = iter(planted_emps)
repeat = next(P)            # gets three cases in the year


def item(d, cat, merchant, amount, **kw):
    return {"expense_date": d, "category": cat, "merchant": merchant, "amount": amount, **kw}


def plant(emp, items, submit, note, checks, approver=None, purpose="Client visit"):
    """checks: list of (check_code, severity, decision, disallowed, escalated, action dict) or [] for 'no case'."""
    c = add_claim(emp, items, submit, approver, purpose, planted=True)
    paid = next(x["paid_date"] for x in claims if x["claim_id"] == c)
    if not checks:
        key.append({"period": f"{paid:%Y-%m}", "claim_id": c, "employee_id": emp["employee_id"], "check_code": "",
                    "severity": "", "expected_decision": "No case", "disallowed": "0.00", "escalated": "False",
                    "note": note})
    for code, sev, decision, disallowed, esc, act in checks:
        key.append({"period": f"{paid:%Y-%m}", "claim_id": c, "employee_id": emp["employee_id"], "check_code": code,
                    "severity": sev, "expected_decision": decision, "disallowed": f"{disallowed:.2f}",
                    "escalated": str(esc), "note": note})
        actions.append({"claim_id": c, "check_code": code, "decision": decision, "disallowed": f"{disallowed:.2f}",
                        "triage_after_wd": act.get("triage", 0), "decide_after_wd": act["decide"],
                        "evidence": act["evidence"], "recover_after_wd": act.get("recover", ""),
                        "recovery_result": act.get("result", ""), "retry_after_wd": act.get("retry", ""),
                        "reviewer_note": act.get("note", "")})
    return c


ok = {"recover": 8, "result": "Recovered"}
# Feb: hotel $260 a night in a standard city, 2 nights (repeat employee, case 1 of 3)
plant(repeat, [item(date(2025, 2, 10), "Hotel", "Grand Residency", 520.00, city="Pune", nights=2)],
      date(2025, 2, 14), "Hotel $260 a night, standard city (v1 cap $200)",
      [("P02", "Medium", "Partly reimbursed", 120.00, False, {"decide": 3, "evidence": "Receipt;Approval record", **ok})])
# Mar: same hotel receipt claimed twice
e = next(P)
plant(e, [item(date(2025, 3, 3), "Hotel", "Metro Suites", 340.00, city="Chennai", nights=2)],
      date(2025, 3, 7), "Original hotel claim", [])
key.pop()  # the original is not a scenario on its own
plant(e, [item(date(2025, 3, 3), "Hotel", "Metro Suites", 340.00, city="Chennai", nights=2)],
      date(2025, 3, 17), "Same hotel claimed twice",
      [("P06", "High", "Rejected", 340.00, False, {"decide": 2, "evidence": "Receipt;Original claim reference", **ok})])
# Mar: claim submitted 80 days after the expense: fine under v1 (90 days)
plant(next(P), [item(date(2025, 1, 6), "Training", "SkillHub", 650.00)],
      date(2025, 3, 27), "Submitted 80 days after the expense (v1 allows 90)", [])
# Apr: claims of $450 and $430 at the same merchant on the same day, both line-manager approved
e = next(P)
plant(e, [item(date(2025, 4, 8), "Other", "Office Mart", 450.00)], date(2025, 4, 10), "Split claim, part 1", [])
key.pop()
plant(e, [item(date(2025, 4, 8), "Other", "Office Mart", 430.00)], date(2025, 4, 11),
      "Split claim: $880 at one merchant on one day, needed a department head",
      [("P07", "High", "Corrected", 0.00, False, {"decide": 2, "evidence": "Department head re-approval;Merchant invoice"})])
# May: business class without pre-approval (economy fare $620)
plant(next(P), [item(date(2025, 5, 12), "Airfare", "SkyJet Airways", 1480.00, travel_class="Business")],
      date(2025, 5, 16), "Business class without pre-approval",
      [("P04", "Medium", "Partly reimbursed", 860.00, False,
        {"decide": 4, "evidence": "Receipt;Economy fare quote", **ok})])
# May: business class covered by a valid exception
e = next(P)
exceptions.append({"exception_id": "X001", "employee_id": e["employee_id"], "rule_code": "P04",
                   "reason": "Long-haul flight, medical advice", "approved_by": e["finance_director_id"],
                   "valid_from": "2025-05-01", "valid_to": "2025-05-31"})
plant(e, [item(date(2025, 5, 20), "Airfare", "Coastal Airlines", 2100.00, travel_class="Business",
               pre_approval_ref="X001")], date(2025, 5, 23), "Business class with a valid exception (X001)", [])
# Jun: hotel $220 a night on 27 Jun (v1 cap $200)
plant(next(P), [item(date(2025, 6, 26), "Hotel", "City Stay", 220.00, city="Jaipur", nights=1)],
      date(2025, 6, 27), "Hotel $220 a night in June (v1 cap $200)",
      [("P02", "Medium", "Partly reimbursed", 20.00, False, {"decide": 2, "evidence": "Receipt", **ok})])
# Jul: hotel $220 a night on 8 Jul (v2 cap $230)
plant(next(P), [item(date(2025, 7, 8), "Hotel", "City Stay", 220.00, city="Jaipur", nights=1)],
      date(2025, 7, 10), "Hotel $220 a night in July (v2 cap $230)", [])
# Jun: hotel $290 a night in London
plant(next(P), [item(date(2025, 6, 9), "Hotel", "Harbour Hotel", 580.00, city="London", nights=2)],
      date(2025, 6, 13), "Hotel $290 a night in London (high-cost cap $300)", [])
# Jul: $1,250 claim approved by the claimant
e = next(P)
plant(e, [item(date(2025, 7, 14), "Training", "ProCert Institute", 1250.00)], date(2025, 7, 16),
      "Claim approved by the claimant",
      [("P08", "High", "Corrected", 0.00, True, {"decide": 2, "evidence": "Department head re-approval"})],
      approver=e["employee_id"])
# Aug: client dinner $900 for 4 attendees
plant(next(P), [item(date(2025, 8, 5), "Client entertainment", "The Oak Room", 900.00, attendees=4, client_present=True)],
      date(2025, 8, 7), "Client dinner $225 per attendee (limit $150)",
      [("P05", "Medium", "Partly reimbursed", 300.00, False, {"decide": 3, "evidence": "Receipt;Attendee list", **ok})])
# Aug: $40 taxi with no receipt; receipt provided in review
plant(next(P), [item(date(2025, 8, 18), "Ground transport", "QuickCab", 40.00, receipt=False)],
      date(2025, 8, 20), "Taxi $40 without receipt; receipt found in review",
      [("P01", "Low", "No breach", 0.00, False, {"decide": 4, "evidence": "Receipt provided late"})])
# Sep: alcohol with no client present ($64 of a $112 dinner)
plant(next(P), [item(date(2025, 9, 9), "Meals", "Urban Bistro", 64.00, alcohol=True),
                item(date(2025, 9, 10), "Ground transport", "Metro Taxi", 48.00)],
      date(2025, 9, 12), "Alcohol with no client present",
      [("P10", "High", "Partly reimbursed", 64.00, False, {"decide": 2, "evidence": "Receipt", **ok})])
# Sep: meals $95 in one day (repeat employee, case 2 of 3)
plant(repeat, [item(date(2025, 9, 15), "Meals", "Spice Route", 55.00),
               item(date(2025, 9, 15), "Meals", "Corner Cafe", 40.00)],
      date(2025, 9, 17), "Meals $95 in one day (cap $75)",
      [("P03", "Low", "Partly reimbursed", 20.00, False, {"decide": 5, "evidence": "Receipt", **ok})])
# Sep: $1,800 claim approved by a line manager
e = next(P)
plant(e, [item(date(2025, 9, 1), "Training", "Learnwise", 1800.00)], date(2025, 9, 3),
      "$1,800 approved by a line manager (needs department head)",
      [("P08", "High", "Corrected", 0.00, True, {"decide": 3, "evidence": "Department head re-approval"})],
      approver=e["line_manager_id"])
# Sep: expense on 15 Jul submitted on 20 Sep (67 days; v2 allows 60); employee on medical leave
e = next(P)
# the reviewer records exception X002 in the register at decision time (not before)
plant(e, [item(date(2025, 7, 15), "Ground transport", "RideNow", 62.00)], date(2025, 9, 22),
      "Submitted 69 days after the expense (v2 allows 60); medical leave",
      [("P09", "Low", "Exception approved", 0.00, False,
        {"decide": 6, "evidence": "Medical leave record;Exception register entry", "note": "Medical leave Jul-Sep; late submission accepted"})])
# Oct: hotel $250 in a standard city, decided late (repeat employee, case 3 of 3)
plant(repeat, [item(date(2025, 10, 6), "Hotel", "Lakeview Inn", 250.00, city="Hyderabad", nights=1)],
      date(2025, 10, 8), "Hotel $250 (v2 cap $230); decided after the due date; third case for this employee",
      [("P02", "Medium", "Partly reimbursed", 20.00, True,
        {"decide": 7, "evidence": "Receipt", **ok, "note": "Reviewer away; decision late"})])
# Nov: upgrade without pre-approval; payroll recovery fails, recovered in December
plant(next(P), [item(date(2025, 11, 3), "Airfare", "Blue Air", 980.00, travel_class="Premium economy")],
      date(2025, 11, 5), "Upgrade without pre-approval; first recovery failed",
      [("P04", "Medium", "Partly reimbursed", 420.00, False,
        {"decide": 3, "evidence": "Receipt;Economy fare quote", "recover": 8, "result": "Failed", "retry": 6})])
# Dec: client lunch over the limit; recovery not received by the end of the run
plant(next(P), [item(date(2025, 12, 8), "Client entertainment", "Saffron Grill", 540.00, attendees=3, client_present=True)],
      date(2025, 12, 10), "Client lunch $180 per attendee; recovery outstanding at the end of the run",
      [("P05", "Medium", "Partly reimbursed", 90.00, False,
        {"decide": 3, "evidence": "Receipt;Attendee list", "recover": 40, "result": "Recovered"})])

# ------------------------------------------------------------------ write
claims.sort(key=lambda r: r["claim_id"])
write(OUT / "employees.csv", employees,
      ["employee_id", "cc_code", "dept_code", "bu_code", "role", "line_manager_id", "dept_head_id", "finance_director_id"])
write(OUT / "policy_rules.csv", policy_rules, ["version", "valid_from", "valid_to", "rule_key", "value"])
write(OUT / "high_cost_cities.csv", [{"city": c} for c in HIGH_COST], ["city"])
write(OUT / "approval_matrix.csv", approval_matrix, ["min_total", "max_total", "approver_role"])
write(OUT / "exception_register.csv", exceptions,
      ["exception_id", "employee_id", "rule_code", "reason", "approved_by", "valid_from", "valid_to"])
write(OUT / "claims.csv", claims, ["claim_id", "employee_id", "submitted_date", "approved_by", "approved_date",
                                   "paid_date", "claim_total", "business_purpose"])
write(OUT / "claim_lines.csv", lines, ["line_id", "claim_id", "expense_date", "category", "merchant", "city", "nights",
                                       "attendees", "amount", "receipt_id", "travel_class", "alcohol",
                                       "client_present", "pre_approval_ref"])
write(OUT / "reviewer_actions.csv", actions, ["claim_id", "check_code", "decision", "disallowed", "triage_after_wd",
                                              "decide_after_wd", "evidence", "recover_after_wd", "recovery_result",
                                              "retry_after_wd", "reviewer_note"])
write(KEY, key, ["period", "claim_id", "employee_id", "check_code", "severity", "expected_decision", "disallowed",
                 "escalated", "note"])
print(f"employees {len(employees)}, claims {len(claims)}, lines {len(lines)}, "
      f"planted rows {len(key)} ({sum(1 for k in key if k['check_code'])} cases)")
