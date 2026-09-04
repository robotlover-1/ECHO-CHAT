package server

import (
	"context"

	zrpc "echo-zrpc-go"
	"keywords-filter/pkg/filter"
	"keywords-filter/proto"
)

type filterService struct {
	proto.UnimplementedFilterServer
	filter filter.IFilter
}

// FilterService is the transport-agnostic surface of the filter service: the
// same instance can be registered on both gRPC (proto.FilterServer) and zrpc.
type FilterService interface {
	proto.FilterServer
	RegisterZRPC(zsrv *zrpc.Server) error
}

func NewFilterService(filter filter.IFilter) FilterService {
	return &filterService{
		filter: filter,
	}
}

func (s *filterService) Validate(_ context.Context, in *proto.FilterReq) (*proto.ValidateRes, error) {
	ok, word := s.filter.Validate(in.Text)
	return &proto.ValidateRes{
		Ok:      ok,
		Keyword: word,
	}, nil
}
func (s *filterService) FindAll(_ context.Context, in *proto.FilterReq) (*proto.FindAllRes, error) {
	words := s.filter.FindAll(in.Text)
	return &proto.FindAllRes{
		Keywords: words,
	}, nil
}
