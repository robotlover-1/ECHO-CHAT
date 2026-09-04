package server

import (
	"context"
	"encoding/json"

	"ai-chat-service/proto"
	zrpc "echo-zrpc-go"
	"echo-zrpc-go/contract"
)

/*
 * zrpc v2 side of the chat service (double-stack with gRPC during the
 * observation period). Unary and stream both reuse the transport-agnostic
 * business path (chatService.ChatCompletion / chatCompletionStream via the
 * ChatStream adapter), so caching / context / keywords / DB / LLM logic is
 * shared unchanged. Auth is enforced by the zrpc server envelope (Bearer).
 */

// ChatSvc 是 chat 业务的 zrpc 单传输出口（gRPC 已删；proto 结构体仅作内部 DTO）。
type ChatSvc interface {
	ChatCompletion(ctx context.Context, in *proto.ChatCompletionRequest) (*proto.ChatCompletionResponse, error)
	ServeChatStreamZRPC(ctx context.Context, raw json.RawMessage, w *zrpc.StreamWriter) error
}

// RegisterChatZRPC wires chat methods on a zrpc.Server.
func RegisterChatZRPC(zsrv *zrpc.Server, chat ChatSvc, streamOK bool) error {
	if err := zsrv.RegisterUnary(contract.MethodChatCompletion, unaryChatAdapter(chat)); err != nil {
		return err
	}
	if streamOK {
		if err := zsrv.RegisterStream(contract.MethodChatCompletionStream, chat.ServeChatStreamZRPC); err != nil {
			return err
		}
	}
	return nil
}

// ServeChatStreamZRPC streams one chat.completion_stream request over zrpc.
func (s *chatService) ServeChatStreamZRPC(ctx context.Context, raw json.RawMessage, w *zrpc.StreamWriter) error {
	var creq contract.ChatCompletionRequest
	if err := json.Unmarshal(raw, &creq); err != nil {
		return zrpc.InvalidArgument(err)
	}
	preq, err := protoReqFromContract(&creq)
	if err != nil {
		return zrpc.InvalidArgument(err)
	}
	zw := &zrpcChatStream{ctx: w.Context(), w: w}
	if err := s.chatCompletionStream(preq, zw); err != nil {
		return err // zrpc-go turns it into an ERROR frame unless already ended
	}
	return zw.End()
}

func unaryChatAdapter(chat ChatSvc) zrpc.UnaryHandler {
	return func(ctx context.Context, raw json.RawMessage) (any, error) {
		var creq contract.ChatCompletionRequest
		if err := json.Unmarshal(raw, &creq); err != nil {
			return nil, zrpc.InvalidArgument(err)
		}
		preq, err := protoReqFromContract(&creq)
		if err != nil {
			return nil, zrpc.InvalidArgument(err)
		}
		presp, err := chat.ChatCompletion(ctx, preq)
		if err != nil {
			return nil, zrpc.Internal(err)
		}
		return contractRespFromProto(presp)
	}
}

// protoReqFromContract converts the shared contract to the gRPC proto request
// (explicit mapping; JSON numbers stay numbers on the wire).
func protoReqFromContract(c *contract.ChatCompletionRequest) (*proto.ChatCompletionRequest, error) {
	out := &proto.ChatCompletionRequest{
		Message:       c.Message,
		Id:            c.ID,
		Pid:           c.PID,
		EnableContext: c.EnableContext,
	}
	if c.ChatParam != nil {
		out.ChatParam = &proto.ChatParam{
			Model:             c.ChatParam.Model,
			MaxTokens:         int32(c.ChatParam.MaxTokens),
			Temperature:       float32(c.ChatParam.Temperature),
			TopP:              float32(c.ChatParam.TopP),
			PresencePenalty:   float32(c.ChatParam.PresencePenalty),
			FrequencyPenalty:  float32(c.ChatParam.FrequencyPenalty),
			BotDesc:           c.ChatParam.BotDesc,
			MinResponseTokens: int32(c.ChatParam.MinResponseTokens),
			ContextTTL:        int32(c.ChatParam.ContextTTL),
			ContextLen:        int32(c.ChatParam.ContextLen),
		}
	}
	return out, nil
}

func contractRespFromProto(p *proto.ChatCompletionResponse) (*contract.ChatCompletionResponse, error) {
	out := &contract.ChatCompletionResponse{
		ID:      p.Id,
		Object:  p.Object,
		Created: p.Created,
		Model:   p.Model,
	}
	for _, ch := range p.Choices {
		if ch == nil {
			continue
		}
		cch := contract.ChatCompletionChoice{
			Index:        int(ch.Index),
			FinishReason: ch.FinishReason,
		}
		if ch.Message != nil {
			cch.Message = contract.ChatCompletionMessage{
				Role:    ch.Message.Role,
				Content: ch.Message.Content,
				Name:    ch.Message.Name,
			}
		}
		out.Choices = append(out.Choices, cch)
	}
	if p.Usage != nil {
		out.Usage = &contract.Usage{
			PromptTokens:     int(p.Usage.PromptTokens),
			CompletionTokens: int(p.Usage.CompletionTokens),
			TotalTokens:      int(p.Usage.TotalTokens),
		}
	}
	return out, nil
}
