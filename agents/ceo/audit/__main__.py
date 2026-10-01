from agents.ceo.audit.data_trust import write_audit

if __name__ == "__main__":
    payload = write_audit()
    print(payload.get("status"), payload.get("generated_at"))
