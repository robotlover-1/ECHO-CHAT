package zrpc

/*
 * Stable status codes, mirroring the C zrpc_status_t wire codes (zrpc_protocol.h).
 * These are part of the public Go contract: callers compare with errors.Is /
 * errors.As on *StatusError, never parse strings.
 */

// Code is a zrpc status code.
type Code int

const (
	CodeOK                 Code = 0
	CodeCancelled          Code = 1
	CodeInvalidArgument    Code = 2
	CodeUnauthenticated    Code = 3
	CodeNotFound           Code = 4
	CodeDeadlineExceeded   Code = 5
	CodeResourceExhausted  Code = 6
	CodeUnavailable        Code = 7
	CodeInternal           Code = 8
	CodeProtocolError      Code = 9
	CodeFrameTooLarge      Code = 10
)

func (c Code) String() string {
	switch c {
	case CodeOK:                return "ok"
	case CodeCancelled:         return "cancelled"
	case CodeInvalidArgument:   return "invalid argument"
	case CodeUnauthenticated:   return "unauthenticated"
	case CodeNotFound:          return "not found"
	case CodeDeadlineExceeded:  return "deadline exceeded"
	case CodeResourceExhausted: return "resource exhausted"
	case CodeUnavailable:       return "unavailable"
	case CodeInternal:          return "internal"
	case CodeProtocolError:     return "protocol error"
	case CodeFrameTooLarge:     return "frame too large"
	default:                    return "unknown"
	}
}

// StatusError carries a stable zrpc status code plus a diagnostic message.
type StatusError struct {
	Code    Code
	Message string
}

func (e *StatusError) Error() string {
	if e == nil {
		return ""
	}
	if e.Message != "" {
		return e.Message
	}
	return e.Code.String()
}

// Is lets errors.Is(err, CodeXxx) work by comparing the code.
func (e *StatusError) Is(target error) bool {
	t, ok := target.(*StatusError)
	if !ok {
		return false
	}
	return t != nil && e.Code == t.Code
}

// InvalidArgument etc. build a *StatusError for handlers to return.
func InvalidArgument(err error) error  { return &StatusError{Code: CodeInvalidArgument, Message: msgOf(err)} }
func NotFound(err error) error        { return &StatusError{Code: CodeNotFound, Message: msgOf(err)} }
func Unauthenticated(err error) error { return &StatusError{Code: CodeUnauthenticated, Message: msgOf(err)} }
func DeadlineExceeded(err error) error{ return &StatusError{Code: CodeDeadlineExceeded, Message: msgOf(err)} }
func ResourceExhausted(err error) error{ return &StatusError{Code: CodeResourceExhausted, Message: msgOf(err)} }
func Unavailable(err error) error     { return &StatusError{Code: CodeUnavailable, Message: msgOf(err)} }
func Internal(err error) error        { return &StatusError{Code: CodeInternal, Message: msgOf(err)} }

func msgOf(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}
