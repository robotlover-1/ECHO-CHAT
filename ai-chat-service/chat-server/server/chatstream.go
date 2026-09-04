package server

import (
	"context"

	"ai-chat-service/proto"
	zrpc "echo-zrpc-go"
	"echo-zrpc-go/contract"
)

/*
 * Transport-agnostic output for the chat stream business. The business path
 * (chatCompletionStream / streamLLMContent) only knows this interface, so the
 * same logic runs over gRPC and zrpc v2.
 */
type ChatStream interface {
	Context() context.Context
	Send(*proto.ChatCompletionStreamResponse) error
}

// grpcChatStream adapts a gRPC server stream to ChatStream (gRPC path).
type grpcChatStream struct {
	proto.Chat_ChatCompletionStreamServer
}

func (g *grpcChatStream) Context() context.Context {
	return g.Chat_ChatCompletionStreamServer.Context()
}

// zrpcChatStream adapts ChatStream onto a zrpc.StreamWriter, converting each
// proto chunk to the shared contract (which the zrpc writer JSON-encodes).
type zrpcChatStream struct {
	ctx context.Context
	w   *zrpc.StreamWriter
}

func (z *zrpcChatStream) Context() context.Context { return z.ctx }

func (z *zrpcChatStream) Send(p *proto.ChatCompletionStreamResponse) error {
	return z.w.Send(contractFromProtoStream(p))
}

func (z *zrpcChatStream) End() error { return z.w.End() }

// contractFromProtoStream converts one stream chunk to the shared contract
// (numeric created, source preserved).
func contractFromProtoStream(p *proto.ChatCompletionStreamResponse) *contract.ChatCompletionStreamResponse {
	out := &contract.ChatCompletionStreamResponse{
		ID: p.Id, Object: p.Object, Created: p.Created, Model: p.Model, Source: p.Source,
	}
	for _, ch := range p.Choices {
		if ch == nil {
			continue
		}
		cch := contract.ChatCompletionStreamChoice{Index: int(ch.Index), FinishReason: ch.FinishReason}
		if ch.Delta != nil {
			cch.Delta = contract.ChatCompletionStreamChoiceDelta{
				Content: ch.Delta.Content,
				Role:    ch.Delta.Role,
			}
		}
		out.Choices = append(out.Choices, cch)
	}
	return out
}
