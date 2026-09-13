"""Run the BillWarden audit headless (no dashboard).

Usage:  python run_audit.py
"""
from billwarden.agent import run_cli

if __name__ == "__main__":
    run_cli()
