from relay.agent.tools import TOOL_SCHEMAS, dispatch_tool

CASES = [
    ("web_search", {"query": "Acme Payments"}),
    ("fetch_page", {"url": "https://acmepay.example/careers"}),
    ("fetch_page", {"url": "not a url"}),
    ("send_email", {"to": "someone@example.com"}),
]


def main() -> None:
    print("menu shown to the model:", [s["name"] for s in TOOL_SCHEMAS])
    print("fetch_page input_schema:", TOOL_SCHEMAS[1]["input_schema"])

    for name, args in CASES:
        result = dispatch_tool(name, args)
        print(f"\n{name} {args} -> ok={result.ok}")
        print("   ", result.content[:150])


if __name__ == "__main__":
    main()
