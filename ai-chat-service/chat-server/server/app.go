package server

import (
	chat_context "ai-chat-service/chat-server/chat-context"
	"ai-chat-service/pkg/config"
	"ai-chat-service/pkg/log"
	"ai-chat-service/pkg/zerror"
	"ai-chat-service/proto"
	"ai-chat-service/services"
	keywords_filter "ai-chat-service/services/keywords-filter"
	keywords_proto "ai-chat-service/services/keywords-filter/proto"
	"ai-chat-service/services/tokenizer"
	"bytes"
	"context"
	"encoding/json"
	"github.com/google/uuid"
	"github.com/sashabaranov/go-openai"
	"net/http"
	"strings"
	"time"
)

const ChatPrimedTokens = 2

type openaiConf struct {
	ApiKey            string
	BaseUrl           string
	Model             string
	MaxTokens         int
	Temperature       float32
	TopP              float32
	PresencePenalty   float32
	FrequencyPenalty  float32
	BotDesc           string
	ContextTTL        int
	ContextLen        int
	MinResponseTokens int
}
type app struct {
	openaiConf *openaiConf
	log        log.ILogger
	// TODO 内容上下文对象
	contextCache chat_context.ContextCache
}

func (s *chatService) newApp(in *proto.ChatCompletionRequest, contextCache chat_context.ContextCache) *app {
	conf := &openaiConf{
		ApiKey:            s.config.Chat.ApiKey,
		BaseUrl:           s.config.Chat.BaseUrl,
		Model:             s.config.Chat.Model,
		MaxTokens:         s.config.Chat.MaxTokens,
		Temperature:       s.config.Chat.Temperature,
		TopP:              s.config.Chat.TopP,
		PresencePenalty:   s.config.Chat.PresencePenalty,
		FrequencyPenalty:  s.config.Chat.FrequencyPenalty,
		BotDesc:           s.config.Chat.BotDesc,
		ContextTTL:        s.config.Chat.ContextTTL,
		ContextLen:        s.config.Chat.ContextLen,
		MinResponseTokens: s.config.Chat.MinResponseTokens,
	}
	if in.ChatParam != nil {
		if in.ChatParam.Model != "" {
			conf.Model = in.ChatParam.Model
		}
		if in.ChatParam.TopP != 0 {
			conf.TopP = in.ChatParam.TopP
		}
		if in.ChatParam.FrequencyPenalty != 0 {
			conf.FrequencyPenalty = in.ChatParam.FrequencyPenalty
		}
		if in.ChatParam.PresencePenalty != 0 {
			conf.PresencePenalty = in.ChatParam.PresencePenalty
		}
		if in.ChatParam.Temperature != 0 {
			conf.Temperature = in.ChatParam.Temperature
		}
		if in.ChatParam.BotDesc != "" {
			conf.BotDesc = in.ChatParam.BotDesc
		}
		if in.ChatParam.MaxTokens != 0 {
			conf.MaxTokens = int(in.ChatParam.MaxTokens)
		}
		if in.ChatParam.ContextTTL != 0 {
			conf.ContextTTL = int(in.ChatParam.ContextTTL)
		}
		if in.ChatParam.ContextLen != 0 {
			conf.ContextLen = int(in.ChatParam.ContextLen)
		}
		if in.ChatParam.MinResponseTokens != 0 {
			conf.MinResponseTokens = int(in.ChatParam.MinResponseTokens)
		}
	}
	return &app{
		openaiConf:   conf,
		log:          s.log,
		contextCache: contextCache,
	}
}
func (a *app) getOpenaiClient() *openai.Client {
	accessToken := a.openaiConf.ApiKey
	config := openai.DefaultConfig(accessToken)
	config.BaseURL = a.openaiConf.BaseUrl
	client := openai.NewClientWithConfig(config)
	return client
}

// streamRawRequest 手动发起流式请求并返回原始响应体。
// 背景：deepseek-v4-flash 是推理模型，复杂问题把答案全部放在 reasoning_content，
// content 始终为空；go-openai 的 stream 会丢弃 reasoning_content，导致前端拿到空回答。
// 这里用原始 SSE 解析，把 reasoning_content 作为 content 的兜底。
func (a *app) streamRawRequest(ctx context.Context, req openai.ChatCompletionRequest) (*http.Response, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return nil, err
	}
	// deepseek-v4-flash 默认开深度推理（reasoning_content，复杂题会无限推理、content 永不出现）。
	// 注入 thinking:disabled 关掉推理 → 直接产正式 content（与网页版一致）。
	// go-openai v1.9.4 没有 thinking 字段，故在序列化后手动注入。
	var m map[string]interface{}
	if err := json.Unmarshal(body, &m); err != nil {
		return nil, err
	}
	m["thinking"] = map[string]interface{}{"type": "disabled"}
	body, err = json.Marshal(m)
	if err != nil {
		return nil, err
	}
	u := a.openaiConf.BaseUrl + "/chat/completions"
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, u, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Authorization", "Bearer "+a.openaiConf.ApiKey)
	return http.DefaultClient.Do(httpReq)
}
func (a *app) buildChatCompletionRequest(in *proto.ChatCompletionRequest, stream bool) (req openai.ChatCompletionRequest, tokens, currTokens int, currMessage openai.ChatCompletionMessage, err error) {
	//当前消息
	currMessage = openai.ChatCompletionMessage{
		Role:    openai.ChatMessageRoleUser,
		Content: in.Message,
	}
	req = openai.ChatCompletionRequest{
		Model: a.openaiConf.Model,
		Messages: []openai.ChatCompletionMessage{
			currMessage,
		},
		MaxTokens:        a.openaiConf.MinResponseTokens,
		Temperature:      a.openaiConf.Temperature,
		TopP:             a.openaiConf.TopP,
		PresencePenalty:  a.openaiConf.PresencePenalty,
		FrequencyPenalty: a.openaiConf.FrequencyPenalty,
		Stream:           stream,
	}
	contextList := make([]*chat_context.ChatMessage, 0)
	if in.EnableContext {
		//从缓存中获取上下文信息
		contextList = a.getContext(in.Pid)
	}
	//重构req.Messages
	tokens, currTokens, req.Messages, err = a.rebuildMessages(contextList, currMessage)
	if err != nil {
		a.log.Error(err)
		return
	}
	req.MaxTokens = a.openaiConf.MaxTokens - tokens
	return
}
func (a *app) rebuildMessages(contextList []*chat_context.ChatMessage, currMessage openai.ChatCompletionMessage) (tokens, currTokens int, messages []openai.ChatCompletionMessage, err error) {
	var sysMessage openai.ChatCompletionMessage
	botTokens := 0
	if a.openaiConf.BotDesc != "" {
		sysMessage = openai.ChatCompletionMessage{
			Role:    openai.ChatMessageRoleSystem,
			Content: a.openaiConf.BotDesc,
		}
		botTokens, err = tokenizer.GetTokens(&sysMessage, a.openaiConf.Model)
		if err != nil {
			a.log.Error(err)
			return
		}
	}
	messages = []openai.ChatCompletionMessage{currMessage}
	currTokens, err = tokenizer.GetTokens(&currMessage, a.openaiConf.Model)
	if err != nil {
		a.log.Error(err)
		return
	}
	if currTokens > a.openaiConf.MaxTokens-a.openaiConf.MinResponseTokens-botTokens-ChatPrimedTokens {
		err = zerror.NewByMsg("请求消息超限")
		a.log.Error(err)
		return
	}
	tokens = currTokens + botTokens + ChatPrimedTokens
	if contextList != nil {
		for _, item := range contextList {
			if tokens+item.Tokens+ChatPrimedTokens > a.openaiConf.MaxTokens-a.openaiConf.MinResponseTokens {
				break
			}
			messages = append(messages, item.Message)
			tokens += item.Tokens + ChatPrimedTokens
		}
	}
	for i, j := 0, len(messages)-1; i < j; i, j = i+1, j-1 {
		messages[i], messages[j] = messages[j], messages[i]
	}
	if botTokens > 0 {
		messages = append([]openai.ChatCompletionMessage{sysMessage}, messages...)
	}
	return
}
func (a *app) buildChatCompletionResponse(msg string) *proto.ChatCompletionResponse {
	res := &proto.ChatCompletionResponse{
		Id:      uuid.New().String(),
		Object:  "chat.completion",
		Created: time.Now().Unix(),
		Model:   a.openaiConf.Model,
		Choices: []*proto.ChatCompletionChoice{
			{
				Message: &proto.ChatCompletionMessage{
					Role:    openai.ChatMessageRoleAssistant,
					Content: msg,
				},
				FinishReason: "stop",
			},
		},
		Usage: &proto.Usage{
			PromptTokens:     0,
			CompletionTokens: 0,
			TotalTokens:      0,
		},
	}
	return res
}

func (a *app) buildChatCompletionStreamResponse(id, delta, finishReason string) *proto.ChatCompletionStreamResponse {
	res := &proto.ChatCompletionStreamResponse{
		Id:      id,
		Object:  "chat.completion.chunk",
		Created: time.Now().Unix(),
		Model:   a.openaiConf.Model,
		Choices: []*proto.ChatCompletionStreamChoice{
			{
				Index: 0,
				Delta: &proto.ChatCompletionStreamChoiceDelta{
					Content: delta,
					Role:    openai.ChatMessageRoleAssistant,
				},
				FinishReason: finishReason,
			},
		},
	}
	return res
}

// withSource 标注响应来源：llm（公有大模型）或 cache（缓存命中）
func (a *app) withSource(res *proto.ChatCompletionStreamResponse, source string) *proto.ChatCompletionStreamResponse {
	res.Source = source
	return res
}

func (a *app) buildChatCompletionStreamResponseList(id, msg string) []*proto.ChatCompletionStreamResponse {
	list := make([]*proto.ChatCompletionStreamResponse, 0)
	// 每 100 字符一个 chunk：逐字符流式对长回答会产生数千个 chunk（8000字符=8000条gRPC/HTTP写入），
	// 拖垮缓存命中链路；改为大块发送显著减少消息数量
	const chunkSize = 100
	runes := []rune(msg)
	for i := 0; i < len(runes); i += chunkSize {
		end := i + chunkSize
		if end > len(runes) {
			end = len(runes)
		}
		list = append(list, a.buildChatCompletionStreamResponse(id, string(runes[i:end]), ""))
	}
	return list
}

func (a *app) getContext(id string) []*chat_context.ChatMessage {
	maxLen := a.openaiConf.ContextLen
	list := make([]*chat_context.ChatMessage, 0, maxLen)
	key := id
	for i := 0; i < maxLen; i++ {
		value, err := a.contextCache.Get(key)
		if err != nil {
			a.log.Error(err)
			return nil
		}
		if value == nil {
			break
		}
		list = append(list, value)
		key = value.PID
	}
	return list
}
func (a *app) saveContext(value *chat_context.ChatMessage) error {
	err := a.contextCache.Set(value.ID, value, a.openaiConf.ContextTTL)
	if err != nil {
		a.log.Error(err)
		return err
	}
	return nil
}

// transportZRPC reports whether a downstream dependency is configured for zrpc.
func transportZRPC(name string) bool {
	cnf := config.GetConfig()
	switch name {
	case "keywords":
		return strings.EqualFold(cnf.DependOn.Keywords.Transport, "zrpc")
	case "sensitive":
		return strings.EqualFold(cnf.DependOn.Sensitive.Transport, "zrpc")
	}
	return false
}

func (a *app) keywords(in *proto.ChatCompletionRequest) []string {
	cnf := config.GetConfig()
	if transportZRPC("keywords") {
		words, err := keywords_filter.ZRPCFindAll(context.Background(),
			cnf.DependOn.Keywords.Address, cnf.DependOn.Keywords.AccessToken, in.Message)
		if err != nil {
			a.log.Error(err)
			return []string{} // fail-open: same as the gRPC error path
		}
		return words
	}

	pool := keywords_filter.GetKeywordsClientPool()
	conn := pool.Get()
	defer pool.Put(conn)
	accessToken := cnf.DependOn.Keywords.AccessToken
	client := keywords_proto.NewFilterClient(conn)
	ctx := services.AppendBearerTokenToContext(context.Background(), accessToken)
	req := &keywords_proto.FilterReq{
		Text: in.Message,
	}
	res, err := client.FindAll(ctx, req)
	if err != nil {
		a.log.Error(err)
		return []string{}
	}
	return res.Keywords

}
func (a *app) sensitive(in *proto.ChatCompletionRequest) (ok bool, msg string, err error) {
	cnf := config.GetConfig()
	if transportZRPC("sensitive") {
		ok, _, err = keywords_filter.ZRPCValidate(context.Background(),
			cnf.DependOn.Sensitive.Address, cnf.DependOn.Sensitive.AccessToken, in.Message)
		if err != nil {
			a.log.Error(err)
			return false, "", err // fail-closed: same as the gRPC error path
		}
		if !ok {
			msg = "触发到了知识盲区，请换个问题再问"
		}
		return
	}

	pool := keywords_filter.GetSensitiveClientPool()
	conn := pool.Get()
	defer pool.Put(conn)
	accessToken := cnf.DependOn.Sensitive.AccessToken
	client := keywords_proto.NewFilterClient(conn)
	ctx := services.AppendBearerTokenToContext(context.Background(), accessToken)
	req := &keywords_proto.FilterReq{
		Text: in.Message,
	}
	res, err := client.Validate(ctx, req)
	if err != nil {
		a.log.Error(err)
		return false, "", err
	}
	ok = res.Ok
	if !ok {
		msg = "触发到了知识盲区，请换个问题再问"
	}
	return
}
