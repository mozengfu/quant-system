-- MySQL dump 10.13  Distrib 9.3.0, for macos11.6 (arm64)
--
-- Host: 127.0.0.1    Database: quant_db
-- ------------------------------------------------------
-- Server version	9.3.0

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `sim_positions`
--

DROP TABLE IF EXISTS `sim_positions`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `sim_positions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `ts_code` varchar(20) NOT NULL,
  `stock_name` varchar(50) NOT NULL,
  `market` enum('sz','sh') NOT NULL,
  `shares` int NOT NULL,
  `cost_price` decimal(8,3) NOT NULL,
  `total_cost` decimal(12,2) NOT NULL,
  `current_price` decimal(8,3) DEFAULT NULL,
  `market_value` decimal(12,2) DEFAULT NULL,
  `profit_loss` decimal(10,2) DEFAULT '0.00',
  `profit_pct` decimal(8,4) DEFAULT '0.0000',
  `ml_prob` decimal(5,3) DEFAULT NULL,
  `stop_loss` decimal(10,3) DEFAULT '0.000',
  `take_profit` decimal(10,3) DEFAULT '0.000',
  `buy_date` date NOT NULL,
  `strategy` varchar(30) DEFAULT NULL,
  `market_state` varchar(20) DEFAULT NULL,
  `buy_time` datetime NOT NULL,
  `status` enum('HOLD','SOLD') NOT NULL DEFAULT 'HOLD',
  `sell_date` date DEFAULT NULL,
  `sell_price` decimal(8,3) DEFAULT NULL,
  `final_pnl` decimal(10,2) DEFAULT NULL,
  `final_pnl_pct` decimal(8,4) DEFAULT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_ts_code` (`ts_code`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB AUTO_INCREMENT=46 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `sim_positions`
--
-- WHERE:  status='HOLD' AND ts_code IN ('002171.SZ','600707.SH')

LOCK TABLES `sim_positions` WRITE;
/*!40000 ALTER TABLE `sim_positions` DISABLE KEYS */;
INSERT INTO `sim_positions` VALUES (37,'600707.SH','彩虹股份','sh',2000,12.200,24400.00,12.450,24900.00,500.00,0.0205,NULL,11.346,134.078,'2026-06-09',NULL,NULL,'2026-06-09 10:05:13','HOLD',NULL,NULL,NULL,NULL,'2026-06-09 18:05:24'),(38,'600707.SH','彩虹股份','sh',1600,12.430,19888.00,12.450,19920.00,32.00,0.0016,NULL,11.560,136.606,'2026-06-09',NULL,NULL,'2026-06-09 10:10:13','HOLD',NULL,NULL,NULL,NULL,'2026-06-09 18:05:24'),(39,'002171.SZ','楚江新材','sz',1900,13.110,24909.00,13.120,24909.00,0.00,0.0000,0.740,12.310,14.000,'2026-06-09','实时扫描v8',NULL,'2026-06-09 09:55:12','HOLD',NULL,NULL,NULL,NULL,'2026-06-09 23:53:55'),(40,'002171.SZ','楚江新材','sz',1900,13.120,24928.00,13.120,24928.00,0.00,0.0000,0.740,12.330,14.050,'2026-06-09','实时扫描v8',NULL,'2026-06-09 10:00:15','HOLD',NULL,NULL,NULL,NULL,'2026-06-09 23:53:55');
/*!40000 ALTER TABLE `sim_positions` ENABLE KEYS */;
UNLOCK TABLES;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-06-10  0:00:28
