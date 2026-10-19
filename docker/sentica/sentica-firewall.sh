#!/bin/sh
# Build a new chain and swap the jump. The live chain is removed only
# after the new jump is in place, so a restart never forwards without drops.
set -eu
iptables -D DOCKER-USER -j SENTICA_NEW 2>/dev/null || true
iptables -F SENTICA_NEW 2>/dev/null || true
iptables -X SENTICA_NEW 2>/dev/null || true
iptables -N SENTICA_NEW
iptables -A SENTICA_NEW -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -A SENTICA_NEW -s 10.8.0.50/32 -d 1.1.1.1/32 -p udp --dport 53 -j RETURN
iptables -A SENTICA_NEW -s 10.8.0.50/32 -d 1.1.1.1/32 -p tcp --dport 53 -j RETURN
iptables -A SENTICA_NEW -s 10.8.0.50/32 -p tcp --dport 443 -j RETURN
iptables -A SENTICA_NEW -s 10.8.0.18/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.19/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.20/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.22/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.82/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.83/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.84/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.85/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.34/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.35/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.50/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.66/32 -j DROP
iptables -A SENTICA_NEW -s 10.8.0.67/32 -j DROP
iptables -I DOCKER-USER 1 -j SENTICA_NEW
iptables -D DOCKER-USER -j SENTICA 2>/dev/null || true
iptables -F SENTICA 2>/dev/null || true
iptables -X SENTICA 2>/dev/null || true
iptables -E SENTICA_NEW SENTICA
