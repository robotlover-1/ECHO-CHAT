package server

import (
	"testing"

	"ai-chat-service/proto"
	"echo-zrpc-go/contract"
)

// proto stream chunk -> shared contract chunk must preserve every field.
func TestProtoStreamChunkToContract(t *testing.T) {
	p := &proto.ChatCompletionStreamResponse{
		Id: "c-1", Object: "chat.completion.chunk", Created: 1788547200, Model: "deepseek-v4-flash", Source: "llm",
		Choices: []*proto.ChatCompletionStreamChoice{{
			Index: 0,
			Delta: &proto.ChatCompletionStreamChoiceDelta{Content: "你", Role: "assistant"},
		}},
	}
	c := contractFromProtoStream(p)
	if c.ID != p.Id || c.Created != p.Created || c.Source != "llm" || len(c.Choices) != 1 {
		t.Fatalf("head mismatch: %+v", c)
	}
	if c.Choices[0].Delta.Content != "你" || c.Choices[0].Delta.Role != "assistant" {
		t.Fatalf("delta mismatch: %+v", c.Choices[0].Delta)
	}
	_ = contract.ChatCompletionStreamResponse{}
}
