package contract

import (
	"encoding/json"
	"testing"
)

// Golden JSON shape for the chat unary contract (proto json_name fields).
func TestChatContractGoldenJSON(t *testing.T) {
	req := ChatCompletionRequest{
		Message: "你好", ID: "u1", PID: "p1", EnableContext: true,
		ChatParam: &ChatParam{Model: "deepseek-v4-flash", MaxTokens: 100, Temperature: 0.5},
	}
	b, _ := json.Marshal(req)
	want := `{"message":"你好","id":"u1","p_id":"p1","enable_context":true,"chat_param":{"model":"deepseek-v4-flash","max_tokens":100,"temperature":0.5}}`
	if string(b) != want {
		t.Errorf("request JSON:\n got %s\nwant %s", string(b), want)
	}

	resp := ChatCompletionResponse{
		ID: "r1", Object: "chat.completion", Created: 1788547200, Model: "deepseek-v4-flash",
		Choices: []ChatCompletionChoice{{Index: 0, Message: ChatCompletionMessage{Role: "assistant", Content: "hi"}, FinishReason: "stop"}},
		Usage:   &Usage{PromptTokens: 12, CompletionTokens: 3, TotalTokens: 15},
	}
	b, _ = json.Marshal(resp)
	want = `{"id":"r1","object":"chat.completion","created":1788547200,"model":"deepseek-v4-flash","choices":[{"index":0,"message":{"role":"assistant","content":"hi"},"finish_reason":"stop"}],"usage":{"prompt_tokens":12,"completion_tokens":3,"total_tokens":15}}`
	if string(b) != want {
		t.Errorf("response JSON:\n got %s\nwant %s", string(b), want)
	}
}
