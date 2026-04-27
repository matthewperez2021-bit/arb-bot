from config.preflight import run_preflight


def test_preflight_rejects_placeholders_and_missing_key_file(tmp_path):
    missing_pem = tmp_path / "missing.pem"
    res = run_preflight(
        kalshi_api_key_id="your-kalshi-api-key-id-here",
        kalshi_private_key_path=str(missing_pem),
        poly_private_key="0xyour-private-key-here",
        poly_proxy_wallet="0xyour-proxy-wallet-address-here",
        anthropic_api_key="sk-ant-your-key-here",
    )
    assert res.ok is False
    assert any("KALSHI_API_KEY_ID" in e for e in res.errors)
    assert any("private key file not found" in e.lower() for e in res.errors)


def test_preflight_allows_missing_anthropic_as_warning(tmp_path):
    pem = tmp_path / "k.pem"
    pem.write_text("dummy")
    res = run_preflight(
        kalshi_api_key_id="real_key_id",
        kalshi_private_key_path=str(pem),
        poly_private_key="0x" + "1" * 64,
        poly_proxy_wallet="0x" + "2" * 40,
        anthropic_api_key="",
    )
    assert res.ok is True
    assert any("ANTHROPIC_API_KEY" in w for w in res.warnings)

