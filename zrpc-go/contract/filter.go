package contract

/*
 * Filter contract shared by keywords-filter (server) and ai-chat-service
 * (client). JSON field names replicate the proto json_name so golden JSON is
 * byte-identical to the gRPC baseline (keywords-filter/proto/filter.proto):
 *   FilterReq{ text } -> ValidateRes{ ok, keyword } / FindAllRes{ keywords }
 */

// FilterRequest is the request for both Validate and FindAll.
type FilterRequest struct {
	Text string `json:"text"`
}

// ValidateResponse reports whether the text hit a sensitive keyword.
type ValidateResponse struct {
	OK      bool   `json:"ok"`
	Keyword string `json:"keyword"`
}

// FindAllResponse lists all keywords found in the text.
type FindAllResponse struct {
	Keywords []string `json:"keywords"`
}
