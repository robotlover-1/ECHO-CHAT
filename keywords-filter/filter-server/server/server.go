package server

import (
	"context"
	"keywords-filter/pkg/filter"
	"keywords-filter/proto"
)

type filterService struct {
	proto.UnimplementedFilterServer
	filter filter.IFilter
}

func NewFilterService(filter filter.IFilter) proto.FilterServer {
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
