"""Stack check (SCAF-R9): the reply states the generated stack from the TRD and the commands that really ran."""
from app.services.stack_check import stack_check

CLINIC_TRD = {"Frontend Technology Stack": {"framework": "Next.js (React, TypeScript)"},
              "Backend Technology Stack": {"language": "Node.js", "framework": "Express"},
              "Database Technology Stack": {"database": "PostgreSQL"}}
CLINIC_COMMANDS = [{"command": "npx --yes create-next-app@latest frontend --ts --app", "cwd": "."},
                   {"command": "npx --yes express-generator@latest --no-view backend", "cwd": "."},
                   {"command": "npm install prisma @prisma/client --package-lock-only", "cwd": "backend"}]


def test_a_matching_stack_is_confirmed():
    assert stack_check(CLINIC_TRD, CLINIC_COMMANDS) == (
        "**Stack check:** Frontend: Next.js ✓ · Backend: Express ✓ · Database: PostgreSQL ✓")


def test_a_mismatch_is_reported_not_hidden():
    # the misleading reply that started this (ISSUES AGT-27): Vite + uv for a Next.js + Express TRD
    wrong = [{"command": "npx --yes create-vite@latest frontend --template react-ts"},
             {"command": "uv init --package backend"}]
    line = stack_check(CLINIC_TRD, wrong)
    assert "Frontend: Next.js ✗ (not generated)" in line and "Backend: Express ✗ (not generated)" in line
    assert "Database: PostgreSQL (set up when you run it)" in line


def test_phoenix_and_flutter_and_nothing_to_check():
    trd = {"Backend Technology Stack": {"language": "Elixir", "framework": "Phoenix"},
           "Mobile Technology Stack": {"framework": "Flutter"},
           "Database Technology Stack": {"database": "PostgreSQL"}}
    commands = [{"command": "mix phx.new booknook --no-install", "cwd": "backend"},
                {"command": "flutter create --platforms=android,ios app", "cwd": "frontend"}]
    assert stack_check(trd, commands) == "**Stack check:** Backend: Phoenix ✓ · Mobile: Flutter ✓ · Database: PostgreSQL ✓"
    assert stack_check({}, commands) is None and stack_check(trd, []) is None
