// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Valory AG
pragma solidity ^0.8.20;

// These contracts test call order. They do not model FPMM prices.
contract Collateral {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function deposit() external payable {
        balanceOf[msg.sender] += msg.value;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transferFrom(address sender, address recipient, uint256 amount)
        external returns (bool)
    {
        require(allowance[sender][msg.sender] >= amount, "allowance");
        require(balanceOf[sender] >= amount, "balance");
        allowance[sender][msg.sender] -= amount;
        balanceOf[sender] -= amount;
        balanceOf[recipient] += amount;
        return true;
    }
}

contract Market {
    Collateral public collateral;
    event FPMMBuy(address indexed buyer, uint256 investmentAmount,
        uint256 feeAmount, uint256 indexed outcomeIndex, uint256 outcomeTokensBought);

    constructor(Collateral token) {
        collateral = token;
    }

    function buy(uint256 amount, uint256 side, uint256 minOut) external {
        uint256 tokens = amount * 2;
        require(side == 0 && tokens >= minOut, "minimum output");
        require(collateral.transferFrom(msg.sender, address(this), amount));
        emit FPMMBuy(msg.sender, amount, 0, side, tokens);
    }
}

contract Factory {
    function create(bytes32 salt, Collateral collateral) external returns (Market) {
        return new Market{salt: salt}(collateral);
    }

    function predict(bytes32 salt, Collateral collateral) external view returns (address) {
        bytes32 initHash = keccak256(abi.encodePacked(type(Market).creationCode,
            abi.encode(collateral)));
        return address(uint160(uint256(keccak256(abi.encodePacked(
            bytes1(0xff), address(this), salt, initHash)))));
    }
}
