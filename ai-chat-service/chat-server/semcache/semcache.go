package semcache

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"strings"

	"ai-chat-service/pkg/config"
	kredis "ai-chat-service/pkg/db/redis"
	"github.com/redis/go-redis/v9"
)

const dim = 256

// 明文 Q-A 存储在 array 引擎（SET/GET）；向量索引在 hash 引擎（HSET，VSEARCH 只扫 hash）
const qaPrefix = "qa:"
const semPrefix = "semcache:"

type embedResp struct {
	Code      int       `json:"code"`
	Embedding []float64 `json:"embedding"`
	Msg       string    `json:"msg"`
}
type rerankResp struct {
	Code   int     `json:"code"`
	Score  float64 `json:"score"`
	Shared bool    `json:"shared"`
	Msg    string  `json:"msg"`
}

// poolClient 复用 ai-chat-service 已有的 redis 连接池：借出 *redis.Client，
// 单条命令执行后立即归还（避免连接得不到释放）。本包不保留跨请求连接。
type poolClient struct {
	pool   kredis.RedisPool
	client *redis.Client
}

func (c *poolClient) Do(ctx context.Context, args ...interface{}) (interface{}, error) {
	defer c.pool.Put(c.client)
	return c.client.Do(ctx, args...).Result()
}

func getClient() *poolClient {
	pool := kredis.GetPool()
	client := pool.Get()
	return &poolClient{pool: pool, client: client}
}

func embedText(ctx context.Context, text string) ([]float32, error) {
	body, _ := json.Marshal(map[string]string{"text": text})
	resp, err := http.Post(config.GetConfig().DependOn.Tokenizer.Address+"/embed",
		"application/json", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var r embedResp
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return nil, err
	}
	if r.Code != 200 || len(r.Embedding) != dim {
		return nil, fmt.Errorf("embed failed: code=%d dim=%d", r.Code, len(r.Embedding))
	}
	vec := make([]float32, dim)
	for i, v := range r.Embedding {
		vec[i] = float32(v)
	}
	return vec, nil
}

func rerank(ctx context.Context, query, cached string) (float64, bool, error) {
	body, _ := json.Marshal(map[string]string{"query": query, "cached_query": cached})
	resp, err := http.Post(config.GetConfig().DependOn.Tokenizer.Address+"/rerank",
		"application/json", bytes.NewReader(body))
	if err != nil {
		return 0, false, err
	}
	defer resp.Body.Close()
	var r rerankResp
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return 0, false, err
	}
	if r.Code != 200 {
		return 0, false, fmt.Errorf("rerank failed: %s", r.Msg)
	}
	return r.Score, r.Shared, nil
}

func qaKey(query string) string  { return qaPrefix + query }
func semKey(query string) string { return semPrefix + query }

// 向量索引值: [u32 dim][float vec[dim]]（小端），与 kvstore parse_vec 匹配
func encodeVec(vec []float32) []byte {
	buf := make([]byte, 4+len(vec)*4)
	binary.LittleEndian.PutUint32(buf, uint32(len(vec)))
	for i, v := range vec {
		binary.LittleEndian.PutUint32(buf[4+i*4:], math.Float32bits(v))
	}
	return buf
}

// CacheQuery: 嵌入查询 → VSEARCH → 阈值+rerank 校验 → 命中返回明文回答
func CacheQuery(ctx context.Context, query string) (string, bool) {
	cnf := config.GetConfig()
	if !cnf.SemanticCache.Enabled {
		return "", false
	}
	vec, err := embedText(ctx, query)
	if err != nil {
		return "", false
	}
	binVec := make([]byte, dim*4)
	for i, v := range vec {
		binary.LittleEndian.PutUint32(binVec[i*4:], math.Float32bits(v))
	}
	res, err := getClient().Do(ctx, "VSEARCH", strconv.Itoa(dim), binVec, "5")
	if err != nil {
		return "", false
	}
	arr, ok := res.([]interface{})
	if !ok || len(arr) < 2 {
		return "", false
	}
	bestKey := fmt.Sprintf("%v", arr[0])
	score, err := strconv.ParseFloat(fmt.Sprintf("%v", arr[1]), 64)
	if err != nil {
		return "", false
	}
	if score < float64(cnf.SemanticCache.Threshold) {
		return "", false
	}
	// bestKey = "semcache:<缓存问题原文>"，剥前缀取缓存问题
	cachedQ := strings.TrimPrefix(bestKey, semPrefix)
	if cachedQ == bestKey {
		return "", false // 前缀不匹配，忽略（防御）
	}
	// 读明文回答（array 引擎）
	answer, err := getClient().Do(ctx, "GET", qaKey(cachedQ))
	if err != nil {
		return "", false
	}
	ans, ok := answer.(string)
	if !ok {
		return "", false
	}
	rs, shared, err := rerank(ctx, query, cachedQ)
	if err != nil {
		return "", false
	}
	if rs < float64(cnf.SemanticCache.RerankThreshold) || !shared {
		return "", false
	}
	return ans, true
}

// CacheWrite: 嵌入 → SET qa:<问题> <回答>（明文）+ HSET semcache:<问题> <[dim][vec]>
func CacheWrite(ctx context.Context, query, answer string) error {
	cnf := config.GetConfig()
	if !cnf.SemanticCache.Enabled {
		return nil
	}
	vec, err := embedText(ctx, query)
	if err != nil {
		return err
	}
	if _, err := getClient().Do(ctx, "SET", qaKey(query), answer); err != nil {
		return err
	}
	_, err = getClient().Do(ctx, "HSET", semKey(query), encodeVec(vec))
	return err
}
