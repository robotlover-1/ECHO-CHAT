package server

import (
	semcache "ai-chat-service/chat-server/semcache"
	chat_context "ai-chat-service/chat-server/chat-context"
	"ai-chat-service/chat-server/data"
	metrics_bus "ai-chat-service/chat-server/metrics-bus"
	"ai-chat-service/pkg/config"
	"ai-chat-service/pkg/log"
	"ai-chat-service/proto"
	"ai-chat-service/services/tokenizer"
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"github.com/golang/protobuf/jsonpb"
	"github.com/google/uuid"
	"github.com/sashabaranov/go-openai"
	"io"
	"net/http"
	"strings"
	"time"
)

type chatService struct {
	proto.UnimplementedChatServer
	config     *config.Config
	log        log.ILogger
	data       data.IChatRecordsData
	busMetrics *metrics_bus.BusMetrics
}

func NewChatService(data data.IChatRecordsData, config *config.Config, log log.ILogger, busMetrics *metrics_bus.BusMetrics) proto.ChatServer {
	return &chatService{
		config:     config,
		log:        log,
		data:       data,
		busMetrics: busMetrics,
	}
}

func (s *chatService) ChatCompletion(ctx context.Context, in *proto.ChatCompletionRequest) (*proto.ChatCompletionResponse, error) {
	redisContextCache := chat_context.NewRedisCache()
	defer redisContextCache.Close()

	app := s.newApp(in, redisContextCache)
	//敏感词过滤
	ok, msg, err := app.sensitive(in)
	if err != nil {
		s.log.Error(err)
		return nil, err
	}
	if !ok {
		res := app.buildChatCompletionResponse(msg)
		return res, nil
	}

	// 语义缓存：命中直接返回历史答案（不调大模型）
	if cachedAns, hit := semcache.CacheQuery(ctx, in.Message); hit {
		return app.buildChatCompletionResponse(cachedAns), nil
	}

	//关键词提取
	keywords := app.keywords(in)

	client := app.getOpenaiClient()
	req, tokens, currTokens, currMessage, err := app.buildChatCompletionRequest(in, false)
	if err != nil {
		s.busMetrics.ErrQuestionsTotalCounter.Inc()
		s.log.Error(err)
		return nil, err
	}
	resp, err := client.CreateChatCompletion(ctx, req)
	if err != nil {
		s.log.Error(err)
		return nil, err
	}
	res := &proto.ChatCompletionResponse{}
	bytes, err := json.Marshal(resp)
	if err != nil {
		s.log.Error(err)
		return nil, err
	}
	err = jsonpb.UnmarshalString(string(bytes), res)
	if err != nil {
		s.log.Error(err)
		return nil, err
	}
	go func() {
		reqContext := &chat_context.ChatMessage{
			ID:      in.Id,
			PID:     in.Pid,
			Message: currMessage,
			Tokens:  currTokens,
		}
		err := app.saveContext(reqContext)
		if err != nil {
			s.log.Error(err)
			return
		}
		resContext := &chat_context.ChatMessage{
			ID:      resp.ID,
			PID:     reqContext.ID,
			Message: resp.Choices[0].Message,
			Tokens:  resp.Usage.CompletionTokens,
		}
		err = app.saveContext(resContext)
		if err != nil {
			s.log.Error(err)
			return
		}
	}()
	go func() {
		records := &data.ChatRecord{
			UserMsg:         in.Message,
			UserMsgTokens:   currTokens,
			UserMsgKeywords: keywords,
			AIMsg:           resp.Choices[0].Message.Content,
			AIMsgTokens:     resp.Usage.CompletionTokens,
			ReqTokens:       tokens,
			CreateAt:        time.Now().Unix(),
		}
		err := s.data.Add(records)
		if err != nil {
			s.log.Error(err)
			return
		}
		// 空回答不写缓存，避免污染
		if content := resp.Choices[0].Message.Content; content != "" {
			if err := semcache.CacheWrite(context.Background(), in.Message, content); err != nil {
				s.log.Error(err)
			}
		}
	}()
	return res, err
}
func (s *chatService) ChatCompletionStream(in *proto.ChatCompletionRequest, stream proto.Chat_ChatCompletionStreamServer) error {
	redisContextCache := chat_context.NewRedisCache()
	defer redisContextCache.Close()

	app := s.newApp(in, redisContextCache)
	//敏感词过滤
	ok, msg, err := app.sensitive(in)
	if err != nil {
		s.busMetrics.ErrQuestionsTotalCounter.Inc()
		s.log.Error(err)
		return err
	}
	if !ok {
		s.busMetrics.SensitiveQuestionsTotalCounter.Inc()
		resId := uuid.New().String()
		startRes := app.buildChatCompletionStreamResponse(resId, "", "")
		endRes := app.buildChatCompletionStreamResponse(resId, "", "stop")
		err = stream.Send(startRes)
		if err != nil {
			s.log.Error(err)
			return err
		}
		resList := app.buildChatCompletionStreamResponseList(resId, msg)
		for _, res := range resList {
			err = stream.Send(res)
			if err != nil {
				s.log.Error(err)
				return err
			}
		}
		err = stream.Send(endRes)
		if err != nil {
			s.log.Error(err)
			return err
		}
		return nil
	}

	// 语义缓存：命中直接返回历史答案（不调大模型），标注来源 cache
	if cachedAns, hit := semcache.CacheQuery(stream.Context(), in.Message); hit {
		resId := uuid.New().String()
		startRes := app.withSource(app.buildChatCompletionStreamResponse(resId, "", ""), "cache")
		endRes := app.withSource(app.buildChatCompletionStreamResponse(resId, "", "stop"), "cache")
		_ = stream.Send(startRes)
		resList := app.buildChatCompletionStreamResponseList(resId, cachedAns)
		for _, res := range resList {
			_ = stream.Send(app.withSource(res, "cache"))
		}
		_ = stream.Send(endRes)
		return nil
	}

	//关键词提取
	keywords := app.keywords(in)

	req, tokens, currTokens, currMessage, err := app.buildChatCompletionRequest(in, false)
	if err != nil {
		s.busMetrics.ErrQuestionsTotalCounter.Inc()
		s.log.Error(err)
		return err
	}
	// deepseek-v4-flash 是推理模型：复杂问题可能把答案全放 reasoning_content、content 为空，
	// go-openai 的 stream 会丢弃 reasoning_content。改用原始 SSE 解析，content 为空时用 reasoning_content 兜底。
	req.Stream = true
	httpResp, err := app.streamRawRequest(stream.Context(), req)
	if err != nil {
		s.busMetrics.ErrQuestionsTotalCounter.Inc()
		s.log.Error(err)
		return err
	}
	defer httpResp.Body.Close()
	if httpResp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(httpResp.Body, 4096))
		s.log.ErrorF("llm stream status=%d body=%s", httpResp.StatusCode, string(body))
		s.busMetrics.ErrQuestionsTotalCounter.Inc()
		return fmt.Errorf("llm stream status=%d", httpResp.StatusCode)
	}
	scanner := bufio.NewScanner(httpResp.Body)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	completionContent := ""
	resultID := ""
	lastFinish := "" // 记录最后 finish_reason，length=被截断
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if data == "[DONE]" {
			break
		}
		var chunk struct {
			ID      string `json:"id"`
			Choices []struct {
				Delta struct {
					Content          string `json:"content"`
					ReasoningContent string `json:"reasoning_content"`
				} `json:"delta"`
				FinishReason string `json:"finish_reason"`
			} `json:"choices"`
		}
		if err := json.Unmarshal([]byte(data), &chunk); err != nil {
			continue
		}
		if len(chunk.Choices) == 0 {
			continue
		}
		if resultID == "" {
			resultID = chunk.ID
		}
		delta := chunk.Choices[0].Delta
		text := delta.Content
		if text == "" {
			text = delta.ReasoningContent
		}
		completionContent += text
		lastFinish = chunk.Choices[0].FinishReason
		res := app.buildChatCompletionStreamResponse(resultID, text, chunk.Choices[0].FinishReason)
		res.Source = "llm" // 公有大模型回答
		if err := stream.Send(res); err != nil {
			s.log.Error(err)
			return err
		}
	}
	if err := scanner.Err(); err != nil {
		s.log.Error(err)
		return err
	}
	resultMessage := openai.ChatCompletionMessage{
		Role:    openai.ChatMessageRoleAssistant,
		Content: completionContent,
	}
	model := s.config.Chat.Model
	if in.ChatParam != nil && in.ChatParam.Model != "" {
		model = in.ChatParam.Model
	}
	resultTokens, err := tokenizer.GetTokens(&resultMessage, model)
	if err != nil {
		s.busMetrics.ErrQuestionsTotalCounter.Inc()
		s.log.Error(err)
		return err
	}

	go func() {
		reqContext := &chat_context.ChatMessage{
			ID:      in.Id,
			PID:     in.Pid,
			Message: currMessage,
			Tokens:  currTokens,
		}
		err := app.saveContext(reqContext)
		if err != nil {
			s.log.Error(err)
			return
		}
		// resultID 可能为空（流异常/首包无ID），避免以空 ID 写入空 key（ai_chat_service_）
		if resultID == "" {
			return
		}
		resContext := &chat_context.ChatMessage{
			ID:      resultID,
			PID:     reqContext.ID,
			Message: resultMessage,
			Tokens:  resultTokens,
		}
		err = app.saveContext(resContext)
		if err != nil {
			s.log.Error(err)
			return
		}
	}()
	go func() {
		s.busMetrics.QuestionsTotalCounter.Inc()
		records := &data.ChatRecord{
			UserMsg:         in.Message,
			UserMsgTokens:   currTokens,
			UserMsgKeywords: keywords,
			AIMsg:           completionContent,
			AIMsgTokens:     resultTokens,
			ReqTokens:       tokens,
			CreateAt:        time.Now().Unix(),
		}
		err := s.data.Add(records)
		if err != nil {
			s.log.Error(err)
			return
		}
		// 空回答或被截断(length)的回答不写缓存，避免污染
		if completionContent != "" && lastFinish != "length" {
			if err := semcache.CacheWrite(context.Background(), in.Message, completionContent); err != nil {
				s.log.Error(err)
			}
		}
	}()
	return nil
}
