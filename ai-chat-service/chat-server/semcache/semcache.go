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
	"time"

	"ai-chat-service/pkg/config"
	kredis "ai-chat-service/pkg/db/redis"
	"ai-chat-service/pkg/log"
	"github.com/redis/go-redis/v9"
)

const dim = 256

const semanticTimeout = 1500 * time.Millisecond

// 语义服务专用 HTTP client：整体超时兜底，防 semantic 卡死拖住聊天。
var semanticClient = &http.Client{Timeout: semanticTimeout}

// 明文 Q-A 存储在 array 引擎（SET/GET），key 直接用问题原文（便于 redis-cli 直接读）；
// 向量索引在 hash 引擎（HSET，VSEARCH 只扫 hash）
const semPrefix = "semcache:"

// 指纹缓存 key 前缀：semfp:v1:<fingerprint> → <候选问题(query 文本)>
// 只存候选问题，不存答案；命中后再 GET <candidate> 取答（值即 candidate=<query>）。
const semfpPrefix = "semfp:v1:"

// TopK 默认值，config 给定 top_k < 1 时回退。
const defaultTopK = 30

type embedResp struct {
	Code                int     `json:"code"`
	Embedding           []float64 `json:"embedding"`
	BypassCache         bool    `json:"bypass_cache"`
	Subject             string  `json:"subject"`
	SubjectID           string  `json:"subject_id"`
	Fingerprint         string  `json:"fingerprint"`
	FingerprintEligible bool    `json:"fingerprint_eligible"`
	Msg                 string  `json:"msg"`
}
type rerankResp struct {
	Code   int     `json:"code"`
	Score  float64 `json:"score"`
	Shared bool    `json:"shared"`
	Reason string  `json:"reason"`
	Msg    string  `json:"msg"`
}

// embedMeta 汇集 /embed 语义指纹决策所需字段。
type embedMeta struct {
	Vec                 []float32
	Bypass              bool
	Subject             string // subject_text：可为空（主题只靠全句本体兜底命中）；空不完全拦，见 CacheQuery/CacheWrite
	SubjectID           string // subject_id：fp/VSEARCH 的真正钥匙；与 Subject 皆空才整体 miss / 不写 (仅此禁入闸)
	Fingerprint         string
	FingerprintEligible bool
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

func embedMetaOf(ctx context.Context, text string) (*embedMeta, error) {
	body, _ := json.Marshal(map[string]string{"text": text})
	endpoint := config.GetConfig().DependOn.Semantic.Address + "/embed"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := semanticClient.Do(req)
	if err != nil {
		log.ErrorF("semantic_unavailable: %v", err)
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		log.ErrorF("semantic_bad_status: %d", resp.StatusCode)
		return nil, fmt.Errorf("embed status=%d", resp.StatusCode)
	}
	var r embedResp
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		log.ErrorF("invalid_json: %v", err)
		return nil, err
	}
	if r.Code != 200 || len(r.Embedding) != dim {
		log.ErrorF("invalid_embedding_dimension: code=%d dim=%d", r.Code, len(r.Embedding))
		return nil, fmt.Errorf("embed failed: code=%d dim=%d", r.Code, len(r.Embedding))
	}
	m := &embedMeta{
		Bypass:              r.BypassCache,
		Subject:             r.Subject,
		SubjectID:           r.SubjectID,
		Fingerprint:         r.Fingerprint,
		FingerprintEligible: r.FingerprintEligible,
	}
	m.Vec = make([]float32, dim)
	for i, v := range r.Embedding {
		m.Vec[i] = float32(v)
	}
	return m, nil
}

// rerank: decision 复核。reason==ok 表示语义上共享主题，可命中；否则为 miss（含日志原因）。
func rerank(ctx context.Context, query, cached string) (float64, bool, string, error) {
	body, _ := json.Marshal(map[string]string{"query": query, "cached_query": cached})
	endpoint := config.GetConfig().DependOn.Semantic.Address + "/rerank"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return 0, false, "", err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := semanticClient.Do(req)
	if err != nil {
		log.ErrorF("semantic_unavailable: %v", err)
		return 0, false, "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		log.ErrorF("semantic_bad_status: %d", resp.StatusCode)
		return 0, false, "", fmt.Errorf("rerank status=%d", resp.StatusCode)
	}
	var r rerankResp
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		log.ErrorF("invalid_json: %v", err)
		return 0, false, "", err
	}
	if r.Code != 200 {
		log.ErrorF("rerank_failed: %s", r.Msg)
		return 0, false, r.Reason, fmt.Errorf("rerank failed: %s", r.Msg)
	}
	return r.Score, r.Shared, r.Reason, nil
}

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

// fetchAnswer: 读明文回答（array 引擎），key 就是问题原文（cachedQ）。
// 指纹命中只是拿到候选问题文本，真正答案仍要 GET <cachedQ> 取 —— semfp 不存答案。
func fetchAnswer(ctx context.Context, cachedQ string) (string, bool) {
	answer, err := getClient().Do(ctx, "GET", cachedQ)
	if err != nil {
		return "", false
	}
	ans, ok := answer.(string)
	if !ok || ans == "" {
		return "", false
	}
	return ans, true
}

// topKOf: VSEARCH 返回条数上限；config top_k < 1 时回退默认 30。
func topKOf(cnf *config.Config) int {
	k := cnf.SemanticCache.TopK
	if k < 1 {
		k = defaultTopK
	}
	return k
}

// CacheQuery: ①bypass/无主题 miss → ②指纹候选前置（semfp:<fp>）decision 复核，过则秒返
// → ③未中时 VSEARCH Top-K 逐个 candidate GET+rerank(decision) 复核，Top1 被主题冲突拒则试下一个。
func CacheQuery(ctx context.Context, query string) (string, bool) {
	cnf := config.GetConfig()
	if !cnf.SemanticCache.Enabled {
		return "", false
	}
	m, err := embedMetaOf(ctx, query)
	if err != nil {
		return "", false
	}
	if m.Bypass || (m.Subject == "" && m.SubjectID == "") {
		// 缓存准入（安全收窄）：状态修改/上下文依赖 → 不查；Subject 与 SubjectID 皆空（通用 how-to 如
		// “怎么减肥”抽不到本体链路）→ 不查全局缓存。仅 subject_text 为空但 subject_id 非空（主题只靠全句
		// 本体兜底命中，如“使用 C 编写 rbtree”）不被拦，可继续走指纹/VSEARCH —— 真正钥匙是 subject_id。
		return "", false
	}
	// ① 语义指纹命中判卷（首查快路径中的快路径）：fp → 候选问题 → decision 复核
	if cnf.SemanticCache.ExactFingerprintEnabled && m.Fingerprint != "" && m.FingerprintEligible {
		if cand, err := getClient().Do(ctx, "GET", semfpPrefix+m.Fingerprint); err == nil {
			if cq, ok := cand.(string); ok && cq != "" {
				rs, shared, reason, err := rerank(ctx, query, cq)
				if err == nil && shared && rs >= float64(cnf.SemanticCache.RerankThreshold) {
					// decision 复核通过：再取候选题的答案（semfp 不存答案）
					if ans, ok2 := fetchAnswer(ctx, cq); ok2 {
						log.InfoF("semcache_fingerprint_hit: subject=%s", m.SubjectID)
						return ans, true
					}
				} else if err == nil && reason != "ok" {
					log.InfoF("semcache_fingerprint_collision: subject=%s reason=%s", m.SubjectID, reason)
				}
			}
		}
	}
	// ② VSEARCH Top-K：得到扁平数组 (key, score)，防御上限 2*k
	k := topKOf(cnf)
	binVec := make([]byte, dim*4)
	for i, v := range m.Vec {
		binary.LittleEndian.PutUint32(binVec[i*4:], math.Float32bits(v))
	}
	res, err := getClient().Do(ctx, "VSEARCH", strconv.Itoa(dim), binVec, strconv.Itoa(k))
	if err != nil {
		return "", false
	}
	arr, ok := res.([]interface{})
	if !ok || len(arr) < 2 {
		return "", false
	}
	if len(arr) > 2*k {
		arr = arr[:2*k] // VSEARCH 返回数组长度防御上限
	}
	// 遍历 (key, score) 对：逐个过阈值+读答案+rerank(decision)，
	// 返回第一个真正通过的候选（Top1 被主题冲突拒绝时，下一个可能是正确结果）
	for i := 0; i+1 < len(arr); i += 2 {
		bestKey := fmt.Sprintf("%v", arr[i])
		score, err := strconv.ParseFloat(fmt.Sprintf("%v", arr[i+1]), 64)
		if err != nil {
			continue
		}
		if score < float64(cnf.SemanticCache.Threshold) {
			continue
		}
		// bestKey = "semcache:<缓存问题原文>"，剥前缀取缓存问题
		cachedQ := strings.TrimPrefix(bestKey, semPrefix)
		if cachedQ == bestKey {
			continue // 前缀不匹配，忽略（防御）
		}
		ans, ok := fetchAnswer(ctx, cachedQ)
		if !ok {
			continue
		}
		rs, shared, reason, err := rerank(ctx, query, cachedQ)
		if err != nil {
			continue
		}
		// decision 门：需共享主题 + 分数过 rerank 阈值 + reason==ok。
		// reason!=ok（language_conflict/operation 冲突/residual 等）同样视为 miss。
		if !shared || rs < float64(cnf.SemanticCache.RerankThreshold) || reason != "ok" {
			continue // 主题冲突/关键词/非 ok 决策不过 → 试下一个候选
		}
		return ans, true
	}
	return "", false
}

// CacheWrite: 嵌入 → SET <问题> <回答>（明文，key=问题原文）+ HSET semcache:<问题> <[dim][vec]>
// + 若指纹可入则 SET semfp:v1:<fp> <问题>（指纹只存候选问题，不存答案）。
func CacheWrite(ctx context.Context, query, answer string) error {
	cnf := config.GetConfig()
	if !cnf.SemanticCache.Enabled {
		return nil
	}
	m, err := embedMetaOf(ctx, query)
	if err != nil {
		return err
	}
	if m.Bypass || (m.Subject == "" && m.SubjectID == "") {
		// 缓存准入（安全收窄）：状态修改/上下文依赖 → 不写；Subject 与 SubjectID 皆空（通用 how-to 抽不到本体链路）
		// → 不写全局缓存。仅 subject_text 为空但 subject_id 非空（主题只靠全句本体兜底命中，如“使用 C 编写 rbtree”）
		// 不被拦，照常写入以便指纹/VSEARCH 命中 —— 真正钥匙是 subject_id。
		return nil
	}
	if _, err := getClient().Do(ctx, "SET", query, answer); err != nil {
		return err
	}
	if _, err := getClient().Do(ctx, "HSET", semKey(query), encodeVec(m.Vec)); err != nil {
		return err
	}
	if cnf.SemanticCache.ExactFingerprintEnabled && m.Fingerprint != "" && m.FingerprintEligible {
		if _, err := getClient().Do(ctx, "SET", semfpPrefix+m.Fingerprint, query); err != nil {
			log.ErrorF("semcache_fingerprint_write_failed: %v", err)
		}
	}
	return nil
}
