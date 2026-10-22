# Gufo slot payload fixture provenance

These complete, synthetic idle/busy examples follow Gufo revision
`7b2784517f5652860e03c4ff0c61f2d8940139db` (checked 2026-10-20):

- [LlamaSlots implementation](https://github.com/gufo-org/gufo/blob/7b2784517f5652860e03c4ff0c61f2d8940139db/src/cli/serve/http_server.cpp#L1448-L1490)
- [Upstream slot contract assertions](https://github.com/gufo-org/gufo/blob/7b2784517f5652860e03c4ff0c61f2d8940139db/tests/functional/server_metrics.py#L135-L167)

Both `is_processing` and per-session `n_ctx` are explicitly emitted and asserted upstream.
The prose documentation lists only some fields; constructing a response solely from
that abbreviated list is not an accurate reproduction of this release's response.
The integration test verifies idle admission, native context readback, raw streaming,
busy refusal and ordinary unload using these examples. No real Gufo model is launched.
