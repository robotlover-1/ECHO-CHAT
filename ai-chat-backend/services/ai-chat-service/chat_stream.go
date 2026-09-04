package ai_chat_service

import (
	"context"
	"errors"
	"io"

	"ai-chat-backend/services"
	ai_chat_service_proto "ai-chat-backend/services/ai-chat-service/proto"
	grpc_client "ai-chat-backend/services/grpc-client"

	"google.golang.org/grpc"

	zrpc "echo-zrpc-go"
	"echo-zrpc-go/contract"
)

/*
 * Transport-agnostic ChatCompletionStream client for the backend. The gRPC and
 * zrpc v2 paths both present the SAME Recv() that yields the backend proto chunk,
 * so the controller's streaming/statistics/billing loop is transport-free.
 * zrpc runs on ctx.Request.Context() so a browser disconnect cancels the stream
 * and, in turn, the upstream LLM HTTP request in chat-service.
 */

// ChatStream is the minimal surface the controller consumes.
type ChatStream interface {
	Recv() (*ai_chat_service_proto.ChatCompletionStreamResponse, error)
	Close() error
}

// OpenChatStream opens a ChatCompletionStream over grpc or zrpc (config switch).
func OpenChatStream(ctx context.Context, transport, address, token string,
	in *ai_chat_service_proto.ChatCompletionRequest) (ChatStream, error) {

	if transport == "zrpc" {
		return openZRPCStream(ctx, address, token, in)
	}
	return openGRPCStream(ctx, address, token, in)
}

/* ---- gRPC path (unchanged behaviour; kept as the default during observation) */

func openGRPCStream(ctx context.Context, address, token string,
	in *ai_chat_service_proto.ChatCompletionRequest) (ChatStream, error) {
	// shared pool is keyed on config.DependOn.AiChatService.Address (parity with
	// the previous code path); address is kept for signature symmetry.
	_ = address
	shared := GetAiChatServiceClientPool()
	c := shared.Get()
	cc := ai_chat_service_proto.NewChatClient(c)
	authCtx := services.AppendBearerTokenToContext(ctx, token)
	gs, err := cc.ChatCompletionStream(authCtx, in)
	if err != nil {
		shared.Put(c)
		return nil, err
	}
	return &grpcChatStream{shared: shared, conn: c, stream: gs}, nil
}

type grpcChatStream struct {
	shared grpc_client.ClientPool
	conn   *grpc.ClientConn
	stream ai_chat_service_proto.Chat_ChatCompletionStreamClient
}

func (g *grpcChatStream) Recv() (*ai_chat_service_proto.ChatCompletionStreamResponse, error) {
	return g.stream.Recv()
}
func (g *grpcChatStream) Close() error {
	_ = g.stream.CloseSend()
	if g.shared != nil {
		g.shared.Put(g.conn)
	}
	return nil
}

/* ---- zrpc v2 path ---- */

func openZRPCStream(ctx context.Context, address, token string,
	in *ai_chat_service_proto.ChatCompletionRequest) (ChatStream, error) {
	cli, err := zrpc.NewClient(clientOptionsFromAddress(address, token))
	if err != nil {
		return nil, err
	}
	st, err := cli.Stream(ctx, contract.MethodChatCompletionStream,
		contractReqFromProto(in))
	if err != nil {
		cli.Close()
		return nil, err
	}
	return &zrpcChatStream{cli: cli, st: st}, nil
}

func clientOptionsFromAddress(address, token string) zrpc.ClientOptions {
	return zrpc.ClientOptions{Host: hostOf(address), Port: portOf(address), Token: token}
}

type zrpcChatStream struct {
	cli *zrpc.Client
	st  *zrpc.Stream
}

func (z *zrpcChatStream) Recv() (*ai_chat_service_proto.ChatCompletionStreamResponse, error) {
	var c contract.ChatCompletionStreamResponse
	err := z.st.Recv(&c)
	if errors.Is(err, io.EOF) {
		return nil, io.EOF
	}
	if err != nil {
		return nil, err
	}
	return protoFromContractStream(&c), nil
}
func (z *zrpcChatStream) Close() error {
	_ = z.st.Close()
	return z.cli.Close()
}

/* ---- proto <-> shared-contract mappers ---- */

func contractReqFromProto(in *ai_chat_service_proto.ChatCompletionRequest) *contract.ChatCompletionRequest {
	out := &contract.ChatCompletionRequest{
		Message: in.Message, ID: in.Id, PID: in.Pid, EnableContext: in.EnableContext,
	}
	if in.ChatParam != nil {
		out.ChatParam = &contract.ChatParam{
			Model: in.ChatParam.Model, MaxTokens: int(in.ChatParam.MaxTokens),
			Temperature: float64(in.ChatParam.Temperature), TopP: float64(in.ChatParam.TopP),
			PresencePenalty: float64(in.ChatParam.PresencePenalty), FrequencyPenalty: float64(in.ChatParam.FrequencyPenalty),
			BotDesc: in.ChatParam.BotDesc, MinResponseTokens: int(in.ChatParam.MinResponseTokens),
			ContextTTL: int(in.ChatParam.ContextTTL), ContextLen: int(in.ChatParam.ContextLen),
		}
	}
	return out
}

func protoFromContractStream(c *contract.ChatCompletionStreamResponse) *ai_chat_service_proto.ChatCompletionStreamResponse {
	out := &ai_chat_service_proto.ChatCompletionStreamResponse{
		Id: c.ID, Object: c.Object, Created: c.Created, Model: c.Model, Source: c.Source,
	}
	for _, ch := range c.Choices {
		pc := &ai_chat_service_proto.ChatCompletionStreamChoice{Index: int32(ch.Index), FinishReason: ch.FinishReason}
		if ch.Delta.Content != "" || ch.Delta.Role != "" {
			pc.Delta = &ai_chat_service_proto.ChatCompletionStreamChoiceDelta{Content: ch.Delta.Content, Role: ch.Delta.Role}
		}
		out.Choices = append(out.Choices, pc)
	}
	return out
}

// hostOf/portOf reuse the small address parser from the grpc pool helpers.
func hostOf(addr string) string { h, _, _ := splitAddr(addr); return h }
func portOf(addr string) int {
	_, p, ok := splitAddr(addr)
	if !ok {
		return 0
	}
	return p
}

func splitAddr(addr string) (string, int, bool) {
	for i := len(addr) - 1; i >= 0; i-- {
		if addr[i] == ':' {
			var p int
			for j := i + 1; j < len(addr); j++ {
				if addr[j] < '0' || addr[j] > '9' {
					return addr[:i], 0, false
				}
				p = p*10 + int(addr[j]-'0')
			}
			return addr[:i], p, true
		}
	}
	return addr, 0, false
}
