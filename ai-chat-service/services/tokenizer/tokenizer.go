package tokenizer

import (
	"ai-chat-service/pkg/config"
	"bytes"
	"encoding/json"
	"fmt"
	"github.com/sashabaranov/go-openai"
	"net/http"
)

type tokensInfo struct {
	Code   int    `json:"code"`
	Tokens int    `json:"num_tokens"`
	Msg    string `json:"msg"`
}

var httpClient = &http.Client{}

func GetTokens(message *openai.ChatCompletionMessage, model string) (int, error) {
	cnf := config.GetConfig()
	url := fmt.Sprintf("%s/tokenizer/%s", cnf.DependOn.Tokenizer.Address, model)
	info := &tokensInfo{}
	if err := postJSON(url, message, info); err != nil {
		return 0, err
	}
	if info.Code != 200 {
		return 0, fmt.Errorf("%v", info.Msg)
	}
	return info.Tokens, nil
}

func postJSON(url string, requestData *openai.ChatCompletionMessage, responseData *tokensInfo) error {
	requestBody, err := json.Marshal(requestData)
	if err != nil {
		return err
	}
	resp, err := httpClient.Post(url, "application/json", bytes.NewReader(requestBody))
	if err != nil {
		return err
	}
	return json.NewDecoder(resp.Body).Decode(responseData)
}
