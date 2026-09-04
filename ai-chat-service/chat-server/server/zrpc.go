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
 * observation period). The ChatCompletion business method is transport-free
 * (context + proto in/out), so the zrpc handler adapts the shared contract to
 * the existing proto request/response via protojson and reuses it unchanged.
 * Auth is enforced by the zrpc server envelope (Bearer token from config).
 */

// RegisterChatZRPC wires the chat methods on a zrpc.Server. streamOK gates
// chat.completion_stream (Task 7); only unary is registered for Task 6.
func RegisterChatZRPC(zsrv *zrpc.Server, chat proto.ChatServer, streamOK bool) error {
	if err := zsrv.RegisterUnary(contract.MethodChatCompletion, unaryChatAdapter(chat)); err != nil {
		return err
	}
	if streamOK {
		if err := registerStreamAdapter(zsrv, chat); err != nil {
			return err
		}
	}
	return nil
}

func unaryChatAdapter(chat proto.ChatServer) zrpc.UnaryHandler {
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

// registerStreamAdapter is filled in by Task 7.
func registerStreamAdapter(zsrv *zrpc.Server, chat proto.ChatServer) error {
	_ = zsrv
	_ = chat
	return nil
}

// Explicit field mapping keeps the JSON wire format Go-native (created etc. as
// numbers). protojson would string-encode int64, diverging from the OpenAI-style
// JSON the backend already consumes, so we do not use it here.

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
