package controllers

import (
	"ai-chat-backend/pkg/config"
	"ai-chat-backend/pkg/log"
	"ai-chat-backend/services"
	ai_chat_service "ai-chat-backend/services/ai-chat-service"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"time"

	ai_chat_service_proto "ai-chat-backend/services/ai-chat-service/proto"

	"ai-chat-backend/pkg/tokenizer"
	"ai-chat-backend/pkg/users"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	openai "github.com/sashabaranov/go-openai"
	"k8s.io/klog/v2"
)

type ChatService struct {
	config *config.Config
	log    log.ILogger
}

type ChatCompletionParams struct {
	Model                 string        `json:"model"`
	MaxTokens             int           `json:"max_tokens,omitempty"`
	Temperature           float32       `json:"temperature,omitempty"`
	PresencePenalty       float32       `json:"presence_penalty,omitempty"`
	FrequencyPenalty      float32       `json:"frequency_penalty,omitempty"`
	ChatSessionTTL        time.Duration `json:"chat_session_ttl"`
	ChatMinResponseTokens int           `json:"chat_min_response_tokens"`
}

type ChatMessageRequest struct {
	Prompt  string                    `json:"prompt"`
	Options ChatMessageRequestOptions `json:"options"`
}

type ChatMessageRequestOptions struct {
	Name            string `json:"name"`
	ParentMessageId string `json:"parentMessageId"`
}

type ChatMessage struct {
	ID              string                                              `json:"id"`
	Text            string                                              `json:"text"`
	Role            string                                              `json:"role"`
	Name            string                                              `json:"name"`
	Delta           string                                              `json:"delta"`
	Detail          *ai_chat_service_proto.ChatCompletionStreamResponse `json:"detail"`
	TokenCount      int                                                 `json:"tokenCount"`
	ParentMessageId string                                              `json:"parentMessageId"`
	Source          string                                              `json:"source"`
	TokensUsed      int                                                 `json:"tokensUsed"`
	TokensSaved     int                                                 `json:"tokensSaved"`
}

func NewChatService(config *config.Config, log log.ILogger) (*ChatService, error) {
	return &ChatService{
		config: config,
		log:    log,
	}, nil
}

func (chat *ChatService) ChatProcess(ctx *gin.Context) {
	payload := ChatMessageRequest{}
	if err := ctx.BindJSON(&payload); err != nil {
		klog.Error(err)
		ctx.JSON(200, gin.H{
			"status":  "Fail",
			"message": fmt.Sprintf("%v", err),
			"data":    nil,
		})
		return
	}

	deviceID, _ := ctx.Get("device_id")
	if chat.config.Auth.Enabled {
		user, err := users.GetByDeviceID(deviceID.(string))
		if err != nil || user == nil || user.Quota <= 0 {
			ctx.JSON(402, gin.H{"status": "Fail", "message": "额度不足，请充值后再试", "data": nil})
			return
		}
	}

	messageID := uuid.New().String()

	result := ChatMessage{
		ID:              uuid.New().String(),
		Role:            openai.ChatMessageRoleAssistant,
		Text:            "",
		ParentMessageId: messageID,
	}

	aiChatServicePool := ai_chat_service.GetAiChatServiceClientPool()
	conn := aiChatServicePool.Get()
	defer aiChatServicePool.Put(conn)
	ctx1 := services.AppendBearerTokenToContext(context.Background(), chat.config.DependOn.AiChatService.AccessToken)
	in := &ai_chat_service_proto.ChatCompletionRequest{
		Id:            messageID,
		Message:       payload.Prompt,
		Pid:           payload.Options.ParentMessageId,
		EnableContext: false,
		ChatParam: &ai_chat_service_proto.ChatParam{
			Model:             chat.config.Chat.Model,
			MaxTokens:         int32(chat.config.Chat.MaxTokens),
			Temperature:       chat.config.Chat.Temperature,
			TopP:              chat.config.Chat.TopP,
			PresencePenalty:   chat.config.Chat.PresencePenalty,
			FrequencyPenalty:  chat.config.Chat.FrequencyPenalty,
			BotDesc:           chat.config.Chat.BotDesc,
			ContextTTL:        int32(chat.config.Chat.ContextTTL),
			ContextLen:        int32(chat.config.Chat.ContextLen),
			MinResponseTokens: int32(chat.config.Chat.MinResponseTokens),
		},
	}
	if in.Pid != "" {
		in.EnableContext = true
	}

	aiChatServiceClient := ai_chat_service_proto.NewChatClient(conn)
	stream, err := aiChatServiceClient.ChatCompletionStream(ctx1, in)
	if err != nil {
		chat.log.Error(err)
		ctx.JSON(200, gin.H{
			"status":  "Fail",
			"message": fmt.Sprintf("%v", err),
			"data":    nil,
		})
		return
	}
	defer stream.CloseSend()

	firstChunk := true
	chunkCount := 0 // 流式过程中定期刷新 tokens 统计
	ctx.Header("Content-type", "application/octet-stream")
	for {
		rsp, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			// 流结束：统计本轮 tokens，按来源分派（缓存命中不计费、记节省；LLM 计费、记消耗）
			promptMsg := openai.ChatCompletionMessage{Role: openai.ChatMessageRoleUser, Content: payload.Prompt}
			respMsg := openai.ChatCompletionMessage{Role: openai.ChatMessageRoleAssistant, Content: result.Text}
			pt, err1 := tokenizer.GetTokenCount(promptMsg, chat.config.Chat.Model)
			rt, err2 := tokenizer.GetTokenCount(respMsg, chat.config.Chat.Model)
			if err1 == nil && err2 == nil {
				if result.Source == "cache" {
					result.TokensSaved = pt + rt
				} else {
					result.TokensUsed = pt + rt
				}
				if chat.config.Auth.Enabled {
					dv, _ := ctx.Get("device_id")
					if id, ok := dv.(string); ok && result.Source != "cache" {
						if err := users.DeductQuota(id, pt+rt); err != nil {
							chat.log.Error(err)
						}
					}
				}
			} else {
				chat.log.ErrorF("计费 token 统计失败: %v / %v", err1, err2)
			}
			// 末包：把 source 与 tokens 统计带给前端
			bts, err := json.Marshal(result)
			if err != nil {
				klog.Error(err)
				return
			}
			ctx.Writer.Write([]byte("\n"))
			if _, err := ctx.Writer.Write(bts); err != nil {
				klog.Error(err)
				return
			}
			ctx.Writer.Flush()
			return
		}

		if err != nil {
			klog.Error(err)
			ctx.JSON(200, gin.H{
				"status":  "Fail",
				"message": fmt.Sprintf("OpenAI Event Error %v", err),
				"data":    nil,
			})
			return
		}

		if rsp.Id != "" {
			result.ID = rsp.Id
		}

		if rsp.Source != "" {
			result.Source = rsp.Source
		}

		if len(rsp.Choices) > 0 {
			content := rsp.Choices[0].Delta.Content
			result.Delta = content
			if len(content) > 0 {
				result.Text += content
			}
			result.Detail = rsp
		}

		// 流式过程中每 15 个 chunk 刷新一次 tokens 统计，前端实时更新
		chunkCount++
		if chunkCount%15 == 0 && result.Source != "" {
			promptMsg := openai.ChatCompletionMessage{Role: openai.ChatMessageRoleUser, Content: payload.Prompt}
			respMsg := openai.ChatCompletionMessage{Role: openai.ChatMessageRoleAssistant, Content: result.Text}
			pt, err1 := tokenizer.GetTokenCount(promptMsg, chat.config.Chat.Model)
			rt, err2 := tokenizer.GetTokenCount(respMsg, chat.config.Chat.Model)
			if err1 == nil && err2 == nil {
				if result.Source == "cache" {
					result.TokensSaved = pt + rt
				} else {
					result.TokensUsed = pt + rt
				}
			}
		}

		bts, err := json.Marshal(result)
		if err != nil {
			klog.Error(err)
			ctx.JSON(200, gin.H{
				"status":  "Fail",
				"message": fmt.Sprintf("OpenAI Event Marshal Error %v", err),
				"data":    nil,
			})
			return
		}

		if !firstChunk {
			ctx.Writer.Write([]byte("\n"))
		} else {
			firstChunk = false
		}

		if _, err := ctx.Writer.Write(bts); err != nil {
			klog.Error(err)
			return
		}

		ctx.Writer.Flush()
	}
}
