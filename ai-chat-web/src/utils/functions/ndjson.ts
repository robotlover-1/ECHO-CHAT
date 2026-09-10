// 读取「累积式」NDJSON 响应（axios onDownloadProgress 给的 xhr.responseText 是全量累积的）。
//
// 原实现在每个 progress 事件里取 responseText 最后一个 '\n' 之后的内容直接 JSON.parse：
// 后端每帧发的是「累积全文」（ai-chat-backend chat.go），末段帧可达数十 KB，
// 浏览器交付时被切开，于是大多数 progress 事件都落在帧中间 → parse 抛错，
// 又被调用处的空 catch 吞掉 → 这一帧被静默丢弃。
// 结果：文本流到一半就不再更新，直到流结束最后一个完整帧到达才补全，
// 表现为「流式输出一段 → 卡住一会儿 → 剩余内容一次全出」。
//
// 这里改为：每次只解析「最后一个完整行」，末尾半行留到下次事件拼接，绝不丢帧。
export function createNdjsonReader<T>(onFrame: (frame: T) => void) {
  let offset = 0
  let carry = ''

  return (responseText: string) => {
    // 重新发起请求 / 文本被重置时，从头开始
    if (responseText.length < offset) {
      offset = 0
      carry = ''
    }
    // carry 是上次残留的半行，responseText.slice(offset) 是本次新增字节，拼起来才是完整流
    const fresh = carry + responseText.slice(offset)
    offset = responseText.length

    const lastNl = fresh.lastIndexOf('\n')
    if (lastNl === -1) {
      carry = fresh // 还没有完整行
      return
    }
    const prevNl = fresh.lastIndexOf('\n', lastNl - 1)
    const line = fresh.slice(prevNl + 1, lastNl).trim()
    carry = fresh.slice(lastNl + 1)

    if (!line)
      return
    try {
      onFrame(JSON.parse(line) as T)
    }
    catch {
      // 坏帧（理论上不再出现）忽略，不影响后续帧
    }
  }
}
