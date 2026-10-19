#!/bin/sh
# The same block as docs/DEPLOY.md. A second run flushes SENTICA and
# inserts the DOCKER-USER jump only when it is absent.
set -eu
iptables -N SENTICA 2>/dev/null || true
iptables -F SENTICA
iptables -A SENTICA -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -A SENTICA -s 10.8.0.50/32 -d 1.1.1.1/32 -p udp --dport 53 -j RETURN
iptables -A SENTICA -s 10.8.0.50/32 -d 1.1.1.1/32 -p tcp --dport 53 -j RETURN
iptables -A SENTICA -s 10.8.0.50/32 -p tcp --dport 443 -j RETURN
iptables -A SENTICA -s 10.8.0.18/32 -j DROP
iptables -A SENTICA -s 10.8.0.19/32 -j DROP
iptables -A SENTICA -s 10.8.0.20/32 -j DROP
iptables -A SENTICA -s 10.8.0.22/32 -j DROP
iptables -A SENTICA -s 10.8.0.82/32 -j DROP
iptables -A SENTICA -s 10.8.0.83/32 -j DROP
iptables -A SENTICA -s 10.8.0.84/32 -j DROP
iptables -A SENTICA -s 10.8.0.85/32 -j DROP
iptables -A SENTICA -s 10.8.0.34/32 -j DROP
iptables -A SENTICA -s 10.8.0.35/32 -j DROP
iptables -A SENTICA -s 10.8.0.50/32 -j DROP
iptables -A SENTICA -s 10.8.0.66/32 -j DROP
iptables -A SENTICA -s 10.8.0.67/32 -j DROP
iptables -C DOCKER-USER -j SENTICA 2>/dev/null || iptables -I DOCKER-USER 1 -j SENTICA
