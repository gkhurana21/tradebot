package main

import (
	"net"
	"testing"
)

func TestDefaultAddrIsLoopbackOnly(t *testing.T) {
	host, port, err := net.SplitHostPort(defaultAddr)
	if err != nil {
		t.Fatalf("defaultAddr %q is not host:port: %v", defaultAddr, err)
	}
	if host != "127.0.0.1" || port != "8080" {
		t.Fatalf("defaultAddr = %q, want 127.0.0.1:8080", defaultAddr)
	}
	if ip := net.ParseIP(host); ip == nil || !ip.IsLoopback() {
		t.Fatalf("default host %q must be loopback", host)
	}
}
