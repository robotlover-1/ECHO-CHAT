package server

import (
	"context"
	"encoding/json"

	zrpc "echo-zrpc-go"
	"echo-zrpc-go/contract"
)

/*
 * zrpc v2 side of the filter service (double-stack with gRPC during the
 * observation period). These handlers reuse the same business IFilter as the
 * gRPC handlers in server.go, so behaviour is transport-independent. Bearer-token
 * auth is enforced by the zrpc server envelope (accessToken from config).
 */

// RegisterZRPC wires the two filter methods onto a zrpc.Server.
func (s *filterService) RegisterZRPC(zsrv *zrpc.Server) error {
	if err := zsrv.RegisterUnary(contract.MethodFilterValidate, s.validateZRPC); err != nil {
		return err
	}
	return zsrv.RegisterUnary(contract.MethodFilterFindAll, s.findAllZRPC)
}

func (s *filterService) validateZRPC(_ context.Context, raw json.RawMessage) (any, error) {
	var req contract.FilterRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		return nil, zrpc.InvalidArgument(err)
	}
	ok, word := s.filter.Validate(req.Text)
	return &contract.ValidateResponse{OK: ok, Keyword: word}, nil
}

func (s *filterService) findAllZRPC(_ context.Context, raw json.RawMessage) (any, error) {
	var req contract.FilterRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		return nil, zrpc.InvalidArgument(err)
	}
	words := s.filter.FindAll(req.Text)
	if words == nil {
		words = []string{} // keep JSON "[]" (protobuf repeated semantics)
	}
	return &contract.FindAllResponse{Keywords: words}, nil
}
