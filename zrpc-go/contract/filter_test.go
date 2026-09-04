package contract

import (
	"encoding/json"
	"testing"
)

// The JSON shape must stay byte-compatible with the gRPC baseline protobuf
// json_name (keywords-filter/proto/filter.proto). Any re-ordering or rename of
// fields here is a golden-contract break and must be flagged.
func TestFilterContractGoldenJSON(t *testing.T) {
	cases := []struct {
		name string
		in   any
		want string
	}{
		{"request", FilterRequest{Text: "hello 世界"}, `{"text":"hello 世界"}`},
		{"validate-hit", ValidateResponse{OK: true, Keyword: "赌"}, `{"ok":true,"keyword":"赌"}`},
		{"validate-miss", ValidateResponse{OK: false, Keyword: ""}, `{"ok":false,"keyword":""}`},
		{"findall", FindAllResponse{Keywords: []string{"赌", "毒"}}, `{"keywords":["赌","毒"]}`},
		{"findall-empty", FindAllResponse{Keywords: []string{}}, `{"keywords":[]}`},
	}
	for _, c := range cases {
		b, err := json.Marshal(c.in)
		if err != nil {
			t.Fatalf("%s: marshal: %v", c.name, err)
		}
		if string(b) != c.want {
			t.Errorf("%s: got %s want %s", c.name, string(b), c.want)
		}
	}
}
