package security

import "testing"

func TestClientKeyFormat(t *testing.T) {
	raw := FormatClientKey("abc123", "secret_value")
	if raw != "g2a_abc123_secret_value" {
		t.Fatalf("formatted key = %q", raw)
	}
	prefix, ok := SplitClientKey(raw)
	if !ok || prefix != "abc123" {
		t.Fatalf("SplitClientKey(%q) = %q, %v", raw, prefix, ok)
	}
	legacy := "legacy-api-key-1234567890"
	legacyPrefix, ok := SplitClientKey(legacy)
	if !ok || legacyPrefix != "legacy_"+HashToken(legacy)[:24] {
		t.Fatalf("legacy prefix = %q, ok = %v", legacyPrefix, ok)
	}
	for _, value := range []string{"", "short", "g2a_", "g2a__secret", "g2a_prefix_"} {
		if _, ok := SplitClientKey(value); ok {
			t.Fatalf("SplitClientKey(%q) unexpectedly succeeded", value)
		}
	}
}
