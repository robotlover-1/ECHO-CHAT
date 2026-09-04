package server

import (
	zrpc "echo-zrpc-go"
	"keywords-filter/pkg/filter"
)

/*
 * filterService：keywords-filter 业务（敏感词/关键词），gRPC 已删除，仅经 zrpc v2 提供
 * （RegisterZRPC → contract.filter.validate / filter.find_all）。业务 IFilter 不变。
 */

type filterService struct {
	filter filter.IFilter
}

// FilterService 是 zrpc 单传输出口：注册到 *zrpc.Server 即可提供服务。
type FilterService interface {
	RegisterZRPC(zsrv *zrpc.Server) error
}

func NewFilterService(filter filter.IFilter) FilterService {
	return &filterService{filter: filter}
}
