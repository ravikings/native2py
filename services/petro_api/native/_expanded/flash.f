C=======================================================================
C     FLASH    -   PENG-ROBINSON TWO PHASE FLASH
C
C     WRITTEN   11-APR-1991   S. VANDERBERG
C     REVISED   30-JUL-1993   RACHFORD-RICE BRACKETED BEFORE NEWTON
C     REVISED   12-DEC-1996   SUCCESSIVE SUBSTITUTION SWITCHES TO
C                             NEWTON AFTER 25 OUTER ITERATIONS
C
C     COMPONENT DATA COMES FROM /COMPS/ IN PETRO.INC.  LOAD IT WITH
C     CMPSET BEFORE CALLING FLASH2.  THE BINARY INTERACTION MATRIX BIC
C     IS ASSUMED SYMMETRIC AND IS NOT CHECKED.
C=======================================================================
      SUBROUTINE CMPSET (N, Z, PC, TC, W, MW)
C-----------------------------------------------------------------------
C     LOAD COMPONENT DATA.  Z IS NORMALISED IN PLACE.
C-----------------------------------------------------------------------
C     --- native2py: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- native2py: end INCLUDE 'PETRO.INC' ---
      DIMENSION Z(N), PC(N), TC(N), W(N), MW(N)
      DOUBLE PRECISION MW
C
      IF (N .LT. 1 .OR. N .GT. NCMAX) THEN
         IERR = 41
         RETURN
      END IF
      S = ZERO
      DO 100 I = 1, N
         IF (Z(I) .LT. ZERO) THEN
            IERR = 42
            RETURN
         END IF
         S = S + Z(I)
  100 CONTINUE
      IF (S .LT. SMALL) THEN
         IERR = 43
         RETURN
      END IF
C
      DO 120 I = 1, N
         ZFEED(I) = Z(I) / S
         PCRIT(I) = PC(I)
         TCRIT(I) = TC(I)
         ACENT(I) = W(I)
         WTMOL(I) = MW(I)
         DO 110 J = 1, N
            BIC(I,J) = ZERO
  110    CONTINUE
  120 CONTINUE
      NCOMP = N
      RETURN
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE WILSON (P, T, XK)
C-----------------------------------------------------------------------
C     WILSON K VALUE INITIAL GUESS.  T IN DEG R, P IN PSIA.
C-----------------------------------------------------------------------
C     --- native2py: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- native2py: end INCLUDE 'PETRO.INC' ---
      DIMENSION XK(NCMAX)
C
      DO 100 I = 1, NCOMP
         IF (P .LT. SMALL .OR. T .LT. SMALL) THEN
            XK(I) = ONE
         ELSE
            XK(I) = (PCRIT(I)/P)
     &              * DEXP(5.373D0*(ONE + ACENT(I))
     &                     * (ONE - TCRIT(I)/T))
         END IF
  100 CONTINUE
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION RRFUNC (XK, BETA)
C-----------------------------------------------------------------------
C     RACHFORD-RICE OBJECTIVE.  MONOTONE DECREASING IN BETA.
C-----------------------------------------------------------------------
C     --- native2py: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- native2py: end INCLUDE 'PETRO.INC' ---
      DIMENSION XK(NCMAX)
C
      S = ZERO
      DO 100 I = 1, NCOMP
         DEN = ONE + BETA*(XK(I) - ONE)
         IF (DABS(DEN) .LT. SMALL) DEN = DSIGN(SMALL, DEN)
         S = S + ZFEED(I)*(XK(I) - ONE) / DEN
  100 CONTINUE
      RRFUNC = S
      RETURN
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE RRSOLV (XK, BETA, ISTAT)
C-----------------------------------------------------------------------
C     SOLVE RACHFORD-RICE FOR THE VAPOUR MOLE FRACTION BETA.
C     ISTAT  0 = TWO PHASE, 1 = ALL LIQUID, 2 = ALL VAPOUR.
C-----------------------------------------------------------------------
C     --- native2py: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- native2py: end INCLUDE 'PETRO.INC' ---
      DIMENSION XK(NCMAX)
      DATA TOL /1.0D-10/, ITMAX /100/
C
      F0 = RRFUNC(XK, ZERO)
      F1 = RRFUNC(XK, ONE)
      IF (F0 .LE. ZERO) THEN
         BETA  = ZERO
         ISTAT = 1
         RETURN
      END IF
      IF (F1 .GE. ZERO) THEN
         BETA  = ONE
         ISTAT = 2
         RETURN
      END IF
C
C     ---- 30-JUL-1993: BISECT FIRST, THEN NEWTON.  PURE NEWTON FROM
C     ---- BETA = 0.5 WOULD WANDER OUTSIDE [0,1] FOR WIDE BOILING FEEDS.
      BLO = ZERO
      BHI = ONE
      DO 100 IT = 1, 30
         BM = HALF*(BLO + BHI)
         FM = RRFUNC(XK, BM)
         IF (FM .GT. ZERO) THEN
            BLO = BM
         ELSE
            BHI = BM
         END IF
         IF (BHI - BLO .LT. 1.0D-3) GO TO 110
  100 CONTINUE
C
  110 BETA = HALF*(BLO + BHI)
      DO 200 IT = 1, ITMAX
         F  = RRFUNC(XK, BETA)
         DB = 1.0D-8
         FP = (RRFUNC(XK, BETA+DB) - F) / DB
         IF (DABS(FP) .LT. SMALL) GO TO 210
         D  = F/FP
         BETA = BETA - D
         IF (BETA .LT. BLO) BETA = BLO
         IF (BETA .GT. BHI) BETA = BHI
         IF (DABS(D) .LT. TOL) GO TO 210
  200 CONTINUE
      IWARN = IWARN + 1
C
  210 ISTAT = 0
      RETURN
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE PRZFAC (P, T, X, ZLIQ, IPHASE)
C-----------------------------------------------------------------------
C     PENG-ROBINSON COMPRESSIBILITY FOR A MIXTURE OF COMPOSITION X.
C     IPHASE  0 = SMALLEST ROOT (LIQUID), 1 = LARGEST ROOT (VAPOUR).
C     THE CUBIC IS SOLVED IN CLOSED FORM (CARDANO).
C-----------------------------------------------------------------------
C     --- native2py: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- native2py: end INCLUDE 'PETRO.INC' ---
      DIMENSION X(NCMAX), AI(NCMAX), BI(NCMAX)
      DATA RGAS /10.7316D0/
      DATA PI /3.14159265358979D0/
C
      AM = ZERO
      BM = ZERO
      DO 100 I = 1, NCOMP
         IF (TCRIT(I) .LT. SMALL .OR. PCRIT(I) .LT. SMALL) THEN
            IERR = 44
            RETURN
         END IF
         TR = T / TCRIT(I)
         XM = 0.37464D0 + 1.54226D0*ACENT(I) - 0.26992D0*ACENT(I)**2
         AL = (ONE + XM*(ONE - DSQRT(TR)))**2
         AI(I) = 0.45724D0 * RGAS**2 * TCRIT(I)**2 * AL / PCRIT(I)
         BI(I) = 0.07780D0 * RGAS * TCRIT(I) / PCRIT(I)
         BM    = BM + X(I)*BI(I)
  100 CONTINUE
      DO 120 I = 1, NCOMP
      DO 110 J = 1, NCOMP
         AM = AM + X(I)*X(J)*DSQRT(AI(I)*AI(J))*(ONE - BIC(I,J))
  110 CONTINUE
  120 CONTINUE
C
      RT = RGAS * T
      IF (RT .LT. SMALL) THEN
         IERR = 45
         RETURN
      END IF
      AA = AM * P / (RT*RT)
      BB = BM * P / RT
C
      C2 = -(ONE - BB)
      C1 = AA - 3.0D0*BB*BB - 2.0D0*BB
      C0 = -(AA*BB - BB*BB - BB**3)
C
      Q = (3.0D0*C1 - C2*C2) / 9.0D0
      R = (9.0D0*C2*C1 - 27.0D0*C0 - 2.0D0*C2**3) / 54.0D0
      D = Q**3 + R*R
C
      IF (D .GT. ZERO) THEN
C        ---- ONE REAL ROOT
         SD = DSQRT(D)
         S1 = DSIGN(DABS(R + SD)**(ONE/3.0D0), R + SD)
         S2 = DSIGN(DABS(R - SD)**(ONE/3.0D0), R - SD)
         ZLIQ = S1 + S2 - C2/3.0D0
      ELSE
C        ---- THREE REAL ROOTS
         SQ = DSQRT(-Q)
         IF (SQ .LT. SMALL) THEN
            ZLIQ = -C2/3.0D0
            RETURN
         END IF
         ARG = R / (SQ**3)
         IF (ARG .GT.  ONE) ARG =  ONE
         IF (ARG .LT. -ONE) ARG = -ONE
         TH = DACOS(ARG)
         Z1 = 2.0D0*SQ*DCOS(TH/3.0D0)            - C2/3.0D0
         Z2 = 2.0D0*SQ*DCOS((TH+2.0D0*PI)/3.0D0) - C2/3.0D0
         Z3 = 2.0D0*SQ*DCOS((TH+4.0D0*PI)/3.0D0) - C2/3.0D0
         IF (IPHASE .EQ. 0) THEN
            ZLIQ = MIN(Z1, Z2, Z3)
            IF (ZLIQ .LT. BB) ZLIQ = MAX(Z1, Z2, Z3)
         ELSE
            ZLIQ = MAX(Z1, Z2, Z3)
         END IF
      END IF
      RETURN
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE FLASH2 (P, T, BETA, XLIQ, YVAP, ITOUT, ISTAT)
C-----------------------------------------------------------------------
C     ISOTHERMAL TWO PHASE FLASH BY SUCCESSIVE SUBSTITUTION.
C     P PSIA, T DEG R.  ON RETURN BETA IS THE VAPOUR MOLE FRACTION.
C-----------------------------------------------------------------------
C     --- native2py: expanded INCLUDE 'PETRO.INC' ---
C=======================================================================
C     PETRO.INC   -   GLOBAL FLUID PROPERTY COMMON BLOCKS
C
C     ORIGINALLY WRITTEN  14-MAR-1988   R.T. HALSEY    DUNCAN, OK
C     REVISED             02-SEP-1991   ADDED VAZQUEZ-BEGGS RS
C     REVISED             11-JUN-1994   RAISED NCMAX FROM 12 TO 20
C     REVISED             30-JAN-1997   Y2K - NONE REQUIRED IN THIS DECK
C
C     *** DO NOT REORDER THE COMMON BLOCKS.  THE BLANK COMMON IS
C     *** EQUIVALENCED AGAINST THE SCRATCH ARRAY IN SIMCOR AND ANY
C     *** CHANGE HERE WILL SILENTLY CORRUPT THE IMPES SOLVE.
C=======================================================================
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
C
      PARAMETER (NCMAX  = 20)
      PARAMETER (NPMAX  = 3)
      PARAMETER (NTABMX = 50)
      PARAMETER (ZERO   = 0.0D0)
      PARAMETER (ONE    = 1.0D0)
      PARAMETER (HALF   = 0.5D0)
      PARAMETER (SMALL  = 1.0D-12)
      PARAMETER (PATM   = 14.6959D0)
      PARAMETER (TABS   = 459.67D0)
C
C-----FLUID SPECIFICATION -----------------------------------------------
C     NCOMP  - NUMBER OF HYDROCARBON COMPONENTS IN USE
C     API    - STOCK TANK OIL GRAVITY, DEG API
C     SGG    - GAS SPECIFIC GRAVITY (AIR = 1.0)
C     SGW    - BRINE SPECIFIC GRAVITY
C     TRES   - RESERVOIR TEMPERATURE, DEG F
C     PB     - BUBBLE POINT PRESSURE, PSIA (COMPUTED BY PVTBUB)
C
      COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
C
C-----PER COMPONENT DATA ------------------------------------------------
      COMMON /COMPS/ ZFEED(NCMAX), PCRIT(NCMAX), TCRIT(NCMAX),
     &               ACENT(NCMAX), WTMOL(NCMAX), BIC(NCMAX,NCMAX)
C
C-----LAST COMPUTED PVT STATE (SET BY PVTSET, READ BY EVERYTHING) --------
C     THESE ARE DELIBERATELY GLOBAL.  THE 1988 CALLING CONVENTION WAS
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
C
      COMMON /PVTOUT/ BO, BG, BW, RSOL, VISO, VISG, VISW,
     &                RHOO, RHOG, RHOW, ZFAC, CO, CG, CW
C
C-----ERROR / DIAGNOSTIC STATE -----------------------------------------
C     IERR   - 0 = OK, >0 = FATAL, <0 = WARNING (SEE PVTERR)
C     LUNPRT - PRINT UNIT (6 = STDOUT, 7 = .PRT FILE)
C
      COMMON /DIAG/ IERR, IWARN, NITPVT, LUNPRT, LUNDBG, IDBGLV
C
C     --- native2py: end INCLUDE 'PETRO.INC' ---
      DIMENSION XLIQ(NCMAX), YVAP(NCMAX), XK(NCMAX), XKOLD(NCMAX)
      DATA TOL /1.0D-8/, ITMAX /200/
C
      IF (NCOMP .LT. 1) THEN
         IERR = 46
         RETURN
      END IF
C
      CALL WILSON (P, T, XK)
C
      DO 300 IT = 1, ITMAX
         DO 200 I = 1, NCOMP
            XKOLD(I) = XK(I)
  200    CONTINUE
C
         CALL RRSOLV (XK, BETA, ISTAT)
         IF (IERR .NE. 0) RETURN
C
         DO 210 I = 1, NCOMP
            DEN = ONE + BETA*(XK(I) - ONE)
            IF (DABS(DEN) .LT. SMALL) DEN = SMALL
            XLIQ(I) = ZFEED(I) / DEN
            YVAP(I) = XK(I) * XLIQ(I)
  210    CONTINUE
C
         CALL PRZFAC (P, T, XLIQ, ZL, 0)
         CALL PRZFAC (P, T, YVAP, ZV, 1)
         IF (IERR .NE. 0) RETURN
C
C        ---- K UPDATE FROM THE Z RATIO.  A FULL FUGACITY COEFFICIENT
C        ---- UPDATE WAS IN THE 1991 VERSION BUT WAS REPLACED IN 1993
C        ---- BECAUSE THE LN PHI LOOP DOMINATED RUN TIME AND THE FIELD
C        ---- CASES ALL CONVERGED ON THIS FORM.  SEE MEMO HD-93-114.
         DIFF = ZERO
         DO 220 I = 1, NCOMP
            IF (ZV .GT. SMALL .AND. ZL .GT. SMALL) THEN
               XK(I) = XK(I) * (ZL/ZV)**0.10D0
            END IF
            DIFF = DIFF + (XK(I) - XKOLD(I))**2
  220    CONTINUE
C
         ITOUT = IT
         IF (DIFF .LT. TOL) RETURN
  300 CONTINUE
C
      IWARN = IWARN + 1
      IF (LUNPRT .GT. 0) WRITE (LUNPRT,9400) ITMAX, DIFF
      RETURN
 9400 FORMAT (' *** FLASH2 NOT CONVERGED IN',I5,
     &        ' SUBSTITUTIONS, DIFF =',1PE12.4)
      END