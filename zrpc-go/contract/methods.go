package contract

// Wire-level method names shared by every service. These are the stable keys
// routed by the C server method table (zrpc v2), replacing gRPC full-method
// strings. One source of truth for ECHO-CHAT.
const (
	MethodChatCompletion       = "chat.completion"
	MethodChatCompletionStream = "chat.completion_stream"
	MethodFilterValidate       = "filter.validate"
	MethodFilterFindAll        = "filter.find_all"
)
